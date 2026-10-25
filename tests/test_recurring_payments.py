import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.services.crypto import GiftCardDecryptionError
from app.services.order_fulfillment import GiftCardDelivery
from app.services.payments import (
    AcquirerBadResponseError,
    AcquirerConfigurationError,
    AcquirerUnavailableError,
    RecurringPayment,
    create_recurring_payment,
)
from app.services.recurring_payments import (
    RecurringChargeNotAllowed,
    RecurringFulfillmentError,
    RecurringPaymentDeclined,
    charge_subscription,
    enqueue_gift_card_delivery,
    recurring_idempotence_key,
)

AS_OF = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PRICE = Decimal("1500.00")
METHOD_ID = "22e12f66-000f-5000-8000-18db351245c7"
PAYMENT_ID = "2c8f7d00-0000-5000-8000-1a2b3c4d5e6f"


def _expected_key(subscription_id: int, billing_at: datetime) -> str:
    billing_date = billing_at.astimezone(timezone.utc).date().isoformat()
    return str(
        uuid5(
            NAMESPACE_URL,
            f"giftcodes:yookassa:subscription:{subscription_id}:{billing_date}",
        )
    )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/awe",
        yookassa_shop_id="test-shop",
        yookassa_secret_key="test-secret",
    )


class _YooKassaResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = text
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Capture:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.url = None
        self.headers = None
        self.json = None
        self.auth = None
        self.calls = 0

    def client(self, *args, **kwargs):
        capture = self
        capture.auth = kwargs.get("auth")

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def post(self, url, headers=None, json=None):
                capture.calls += 1
                capture.url = url
                capture.headers = headers
                capture.json = json
                return _YooKassaResponse(capture.payload, capture.status_code, text="nope")

        return _Client()


def _payment(
    *,
    status: str = "succeeded",
    paid: bool = True,
    amount: str = "1500.00",
    currency: str = "RUB",
) -> dict:
    return {
        "id": PAYMENT_ID,
        "status": status,
        "paid": paid,
        "amount": {"value": amount, "currency": currency},
    }


def test_recurring_idempotence_key_is_uuid5_of_subscription_and_utc_billing_date():
    assert recurring_idempotence_key(7, AS_OF) == _expected_key(7, AS_OF)
    same_day = datetime(2026, 10, 23, 23, 59, tzinfo=timezone.utc)
    assert recurring_idempotence_key(7, same_day) == _expected_key(7, AS_OF)
    moscow = timezone(timedelta(hours=3))
    # 24 октября 01:30 по Москве — ещё 23 октября в UTC.
    assert recurring_idempotence_key(
        7,
        datetime(2026, 10, 24, 1, 30, tzinfo=moscow),
    ) == _expected_key(7, AS_OF)
    assert recurring_idempotence_key(8, AS_OF) != _expected_key(7, AS_OF)
    assert recurring_idempotence_key(7, AS_OF + timedelta(days=1)) != _expected_key(7, AS_OF)


def test_recurring_idempotence_key_rejects_naive_datetime():
    with pytest.raises(ValueError):
        recurring_idempotence_key(7, datetime(2026, 10, 23, 12, 0))


async def test_create_recurring_payment_posts_unattended_charge():
    capture = _Capture(_payment())
    with patch("app.services.payments.httpx.AsyncClient", capture.client):
        payment = await create_recurring_payment(
            shop_id="test-shop",
            secret_key="test-secret",
            amount_rub=PRICE,
            payment_method_id=METHOD_ID,
            idempotence_key=_expected_key(7, AS_OF),
            description="Подписка №7",
            metadata={"subscription_id": "7", "nominal_usd": "10"},
        )

    assert payment == RecurringPayment(
        external_payment_id=PAYMENT_ID,
        status="succeeded",
        paid=True,
        amount_value=PRICE,
        currency="RUB",
    )
    assert capture.url == "https://api.yookassa.ru/v3/payments"
    assert capture.headers == {"Idempotence-Key": _expected_key(7, AS_OF)}
    assert capture.json == {
        "amount": {"value": "1500.00", "currency": "RUB"},
        "capture": True,
        "payment_method_id": METHOD_ID,
        "description": "Подписка №7",
        "metadata": {"subscription_id": "7", "nominal_usd": "10"},
    }
    assert "confirmation" not in capture.json
    assert "save_payment_method" not in capture.json
    request = httpx.Request("POST", capture.url)
    next(capture.auth.auth_flow(request))
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(
        b"test-shop:test-secret"
    ).decode()


async def test_create_recurring_payment_rejects_missing_credentials_and_bad_responses():
    with pytest.raises(AcquirerConfigurationError):
        await create_recurring_payment(
            shop_id="",
            secret_key="secret",
            amount_rub=PRICE,
            payment_method_id=METHOD_ID,
            idempotence_key="key",
            description="Подписка №1",
            metadata={"subscription_id": "1"},
        )

    capture = _Capture(status_code=500)
    with (
        patch("app.services.payments.httpx.AsyncClient", capture.client),
        pytest.raises(AcquirerBadResponseError),
    ):
        await create_recurring_payment(
            shop_id="shop",
            secret_key="secret",
            amount_rub=PRICE,
            payment_method_id=METHOD_ID,
            idempotence_key="key",
            description="Подписка №1",
            metadata={"subscription_id": "1"},
        )

    class _Down:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("down")

    with (
        patch("app.services.payments.httpx.AsyncClient", _Down),
        pytest.raises(AcquirerUnavailableError),
    ):
        await create_recurring_payment(
            shop_id="shop",
            secret_key="secret",
            amount_rub=PRICE,
            payment_method_id=METHOD_ID,
            idempotence_key="key",
            description="Подписка №1",
            metadata={"subscription_id": "1"},
        )


async def _seed(
    session,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    dunning_attempts: int = 2,
    cards: int = 1,
    billing_at: datetime = AS_OF,
):
    user = User(email="subscriber@example.com")
    session.add(user)
    session.add(Price(nominal_usd=10, price_rub=PRICE))
    await session.flush()
    for _ in range(cards):
        session.add(
            GiftCard(
                nominal_usd=10,
                code_encrypted=b"ciphertext",
                status=GiftCardStatus.available,
            )
        )
    subscription = Subscription(
        user_id=user.id,
        nominal_usd=10,
        payment_method_id=METHOD_ID,
        status=status,
        next_billing_at=billing_at,
        dunning_attempts=dunning_attempts,
    )
    session.add(subscription)
    await session.commit()
    return user, subscription


def _patches(capture: _Capture, decrypt=None):
    decrypt_patch = patch(
        "app.services.recurring_payments.decrypt_code",
        return_value="GIFT-1" if decrypt is None else decrypt,
        side_effect=decrypt if isinstance(decrypt, Exception) else None,
    )
    return (
        patch("app.services.recurring_payments.get_settings", return_value=_settings()),
        patch("app.services.payments.httpx.AsyncClient", capture.client),
        decrypt_patch,
        patch("app.services.recurring_payments.enqueue_gift_card_delivery"),
    )


async def test_charge_subscription_creates_paid_order_sells_card_and_queues_email(db_session):
    user, subscription = await _seed(db_session, cards=2)
    capture = _Capture(_payment())
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch as enqueue:
        result = await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    orders = list((await db_session.scalars(select(Order))).all())
    cards = list(
        (await db_session.scalars(select(GiftCard).order_by(GiftCard.id))).all()
    )

    assert result.order_id == orders[0].id
    assert result.subscription_id == subscription.id
    assert result.external_payment_id == PAYMENT_ID
    assert capture.calls == 1
    assert capture.url == "https://api.yookassa.ru/v3/payments"
    assert capture.headers["Idempotence-Key"] == _expected_key(subscription.id, AS_OF)
    assert capture.json["capture"] is True
    assert capture.json["payment_method_id"] == METHOD_ID
    assert "confirmation" not in capture.json
    assert capture.json["amount"] == {"value": "1500.00", "currency": "RUB"}
    assert capture.json["metadata"]["subscription_id"] == str(subscription.id)

    assert len(orders) == 1
    order = orders[0]
    assert order.user_id == user.id
    assert order.status is OrderStatus.paid
    assert order.paid_at is not None
    assert order.total_amount_rub == PRICE
    assert order.items == [
        {"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"}
    ]

    sold = [card for card in cards if card.status is GiftCardStatus.sold]
    available = [card for card in cards if card.status is GiftCardStatus.available]
    assert len(sold) == 1
    assert len(available) == 1
    assert sold[0].order_id == order.id
    assert sold[0].sale_price_rub == PRICE

    assert subscription.next_billing_at == AS_OF + timedelta(days=30)
    assert subscription.dunning_attempts == 0
    enqueue.assert_called_once()
    delivery = enqueue.call_args.args[0]
    assert delivery.order_id == order.id
    assert delivery.recipient_email == "subscriber@example.com"
    assert delivery.gift_card_codes == ("GIFT-1",)
    assert delivery.total_amount_rub == PRICE

    second = _Capture(_payment())
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(second)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch:
        await charge_subscription(db_session, subscription)
    assert second.headers["Idempotence-Key"] == _expected_key(
        subscription.id,
        AS_OF + timedelta(days=30),
    )


async def test_declined_payment_keeps_stock_and_billing_date(db_session):
    _, subscription = await _seed(db_session)
    capture = _Capture(_payment(status="canceled", paid=False))
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch as enqueue:
        with pytest.raises(RecurringPaymentDeclined):
            await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert subscription.next_billing_at == AS_OF
    assert subscription.dunning_attempts == 2
    assert list((await db_session.scalars(select(Order))).all()) == []
    card = await db_session.scalar(select(GiftCard))
    assert card.status is GiftCardStatus.available
    assert card.order_id is None
    enqueue.assert_not_called()


async def test_amount_mismatch_does_not_fulfill(db_session):
    _, subscription = await _seed(db_session)
    capture = _Capture(_payment(amount="10.00"))
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch as enqueue:
        with pytest.raises(RecurringPaymentDeclined):
            await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert subscription.next_billing_at == AS_OF
    assert subscription.dunning_attempts == 2
    assert list((await db_session.scalars(select(Order))).all()) == []
    enqueue.assert_not_called()


async def test_acquirer_error_does_not_create_order(db_session):
    _, subscription = await _seed(db_session)
    capture = _Capture(status_code=500)
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch as enqueue:
        with pytest.raises(AcquirerBadResponseError):
            await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert subscription.next_billing_at == AS_OF
    assert list((await db_session.scalars(select(Order))).all()) == []
    enqueue.assert_not_called()


async def test_empty_stock_rolls_back_and_keeps_idempotence_period(db_session):
    _, subscription = await _seed(db_session, cards=0)
    capture = _Capture(_payment())
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch as enqueue:
        with pytest.raises(RecurringFulfillmentError):
            await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert capture.calls == 1
    assert capture.headers["Idempotence-Key"] == _expected_key(subscription.id, AS_OF)
    assert subscription.next_billing_at == AS_OF
    assert subscription.dunning_attempts == 2
    assert list((await db_session.scalars(select(Order))).all()) == []
    enqueue.assert_not_called()


async def test_undecryptable_card_rolls_back(db_session):
    _, subscription = await _seed(db_session)
    capture = _Capture(_payment())
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(
        capture,
        decrypt=GiftCardDecryptionError("bad"),
    )
    with settings_patch, client_patch, decrypt_patch, enqueue_patch:
        with pytest.raises(RecurringFulfillmentError):
            await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert subscription.next_billing_at == AS_OF
    card = await db_session.scalar(select(GiftCard))
    assert card.status is GiftCardStatus.available
    assert card.order_id is None
    assert list((await db_session.scalars(select(Order))).all()) == []


async def test_inactive_subscription_is_not_charged(db_session):
    _, subscription = await _seed(db_session, status=SubscriptionStatus.cancelled)
    capture = _Capture(_payment())
    settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
    with settings_patch, client_patch, decrypt_patch, enqueue_patch:
        with pytest.raises(RecurringChargeNotAllowed):
            await charge_subscription(db_session, subscription)

    assert capture.calls == 0
    await db_session.refresh(subscription)
    assert subscription.next_billing_at == AS_OF
    assert subscription.dunning_attempts == 2


async def test_stale_billing_date_does_not_charge_again(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_maker() as setup:
        _, subscription = await _seed(setup)
        subscription_id = subscription.id

    async with session_maker() as worker, session_maker() as other:
        loaded = await worker.get(Subscription, subscription_id)
        assert loaded is not None
        moved = await other.get(Subscription, subscription_id)
        assert moved is not None
        moved.next_billing_at = AS_OF + timedelta(days=30)
        await other.commit()

        capture = _Capture(_payment())
        settings_patch, client_patch, decrypt_patch, enqueue_patch = _patches(capture)
        with settings_patch, client_patch, decrypt_patch, enqueue_patch:
            with pytest.raises(RecurringChargeNotAllowed):
                await charge_subscription(worker, loaded)

        assert capture.calls == 0
        await worker.refresh(loaded)
        assert loaded.next_billing_at == AS_OF + timedelta(days=30)
        assert list((await worker.scalars(select(Order))).all()) == []


async def test_enqueue_gift_card_delivery_schedules_email_and_delivered_mark():
    delivery = GiftCardDelivery(
        order_id=41,
        recipient_email="subscriber@example.com",
        gift_card_codes=("GIFT-1",),
        total_amount_rub=PRICE,
    )
    with (
        patch(
            "app.services.recurring_payments.send_gift_card_code_email",
            new_callable=AsyncMock,
        ) as send_email,
        patch(
            "app.services.recurring_payments.mark_order_delivered",
            new_callable=AsyncMock,
        ) as mark_delivered,
        patch(
            "app.services.recurring_payments.notify_low_stock_task",
            new_callable=AsyncMock,
        ) as low_stock,
    ):
        enqueue_gift_card_delivery(delivery)
        await _wait_for_background_tasks()

    send_email.assert_awaited_once_with(delivery)
    mark_delivered.assert_awaited_once_with(41)
    low_stock.assert_awaited_once_with()


async def _wait_for_background_tasks() -> None:
    import asyncio

    await asyncio.sleep(0)
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("gift-card-")
    ]
    if tasks:
        await asyncio.gather(*tasks)
