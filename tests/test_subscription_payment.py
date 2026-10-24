import importlib
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import get_db
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.payment_webhook import VerifiedPayment, YooKassaNotification
from app.services.order_checkout import create_one_time_order_checkout
from app.services.order_fulfillment import (
    OrderFulfillmentUnavailable,
    fulfill_verified_payment,
)
from app.services.payment_webhooks import verify_yookassa_notification
from app.services.payments import create_payment

with patch.dict(
    os.environ,
    {"DATABASE_URL": "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/awe"},
):
    create_app = importlib.import_module("app.main").create_app

PAYMENT_ID = "2c8f7d00-0000-5000-8000-1a2b3c4d5e6f"
SAVED_METHOD_ID = "22e12f66-000f-5000-8000-18db351245c7"
PRICE = Decimal("1500.00")


class _FakeSession:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class _YooKassaResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.is_server_error = status_code >= 500
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _payment_body(
    order_id: int,
    *,
    subscription: bool,
    method_id: str | None,
    saved: bool | None = True,
) -> dict:
    metadata = {"order_id": str(order_id)}
    if subscription:
        metadata["is_subscription"] = "true"
    body = {
        "id": PAYMENT_ID,
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "1500.00", "currency": "RUB"},
        "metadata": metadata,
    }
    if method_id is not None:
        method: dict = {"type": "bank_card", "id": method_id}
        if saved is not None:
            method["saved"] = saved
        body["payment_method"] = method
    return body


def _notification(order_id: int, *, subscription: bool, method_id: str | None) -> dict:
    return {
        "type": "notification",
        "event": "payment.succeeded",
        "object": _payment_body(
            order_id,
            subscription=subscription,
            method_id=method_id,
            saved=False,
        ),
    }


class _YooKassaApi:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.gets = 0

    def client(self, *args, **kwargs):
        api = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url):
                api.gets += 1
                return _YooKassaResponse(api.payload)

        return _Client()


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/awe",
        yookassa_shop_id="test-shop",
        yookassa_secret_key="test-secret",
        payment_return_url="https://example.test/payment/complete",
    )


class _CreatePaymentCapture:
    def __init__(self) -> None:
        self.json = None
        self.headers = None

    def client(self, *args, **kwargs):
        capture = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def post(self, url, headers=None, json=None):
                capture.json = json
                capture.headers = headers
                return _YooKassaResponse(
                    {
                        "id": "pay-1",
                        "confirmation": {
                            "confirmation_url": "https://yookassa.test/pay/1",
                        },
                    }
                )

        return _Client()


async def _seed_pending_order(session, email: str = "subscriber@example.com"):
    user = User(email=email)
    session.add(user)
    session.add(Price(nominal_usd=10, price_rub=PRICE))
    await session.flush()
    order = Order(
        user_id=user.id,
        status=OrderStatus.pending_payment,
        items=[{"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"}],
        total_amount_rub=PRICE,
    )
    session.add(order)
    await session.flush()
    session.add(
        GiftCard(
            nominal_usd=10,
            code_encrypted=b"ciphertext",
            status=GiftCardStatus.reserved,
            order_id=order.id,
            sale_price_rub=PRICE,
        )
    )
    await session.commit()
    return user, order


async def test_create_payment_sends_save_payment_method_for_subscription():
    capture = _CreatePaymentCapture()
    with patch("app.services.payments.httpx.AsyncClient", capture.client):
        session = await create_payment(
            shop_id="test-shop",
            secret_key="test-secret",
            order_id=42,
            amount_rub=PRICE,
            return_url="https://example.test/payment/complete",
            save_payment_method=True,
        )

    assert session.external_payment_id == "pay-1"
    assert capture.json["save_payment_method"] is True
    assert capture.json["capture"] is True
    assert capture.json["metadata"] == {
        "order_id": "42",
        "is_subscription": "true",
    }
    assert capture.json["amount"] == {"value": "1500.00", "currency": "RUB"}
    assert capture.headers["Idempotence-Key"]


async def test_create_payment_omits_save_flag_for_one_time_order():
    capture = _CreatePaymentCapture()
    with patch("app.services.payments.httpx.AsyncClient", capture.client):
        await create_payment(
            shop_id="test-shop",
            secret_key="test-secret",
            order_id=42,
            amount_rub=PRICE,
            return_url="https://example.test/payment/complete",
        )

    assert "save_payment_method" not in capture.json
    assert capture.json["metadata"] == {"order_id": "42"}


async def test_checkout_requests_saved_payment_method_only_for_subscription():
    db = _FakeSession()
    settings = _settings()
    order = SimpleNamespace(id=42, total_amount_rub=PRICE)
    with (
        patch(
            "app.services.order_checkout.create_one_time_order",
            new_callable=AsyncMock,
        ) as mock_order,
        patch(
            "app.services.order_checkout.create_payment",
            new_callable=AsyncMock,
        ) as mock_payment,
    ):
        mock_order.return_value = order
        mock_payment.return_value = SimpleNamespace(
            payment_url="https://yookassa.test/pay/1",
            external_payment_id="pay-1",
        )
        await create_one_time_order_checkout(
            db,
            email="buyer@example.com",
            nominal_usd=10,
            settings=settings,
            is_subscription=True,
        )

    mock_payment.assert_awaited_once_with(
        shop_id="test-shop",
        secret_key="test-secret",
        order_id=42,
        amount_rub=PRICE,
        return_url="https://example.test/payment/complete",
        save_payment_method=True,
    )


def test_order_endpoint_forwards_subscription_flag():
    session = _FakeSession()
    settings = _settings()
    app = create_app()

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        with patch(
            "app.api.v1.orders.create_one_time_order_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout:
            mock_checkout.return_value = SimpleNamespace(
                order_id=42,
                payment_url="https://yookassa.test/pay/1",
            )
            subscribed = client.post(
                "/api/v1/orders/one-time",
                json={
                    "email": "buyer@example.com",
                    "nominal_usd": 10,
                    "is_subscription": True,
                },
            )
            subscribed_call = mock_checkout.await_args

            one_time = client.post(
                "/api/v1/orders/one-time",
                json={"email": "buyer@example.com", "nominal_usd": 10},
            )
            one_time_call = mock_checkout.await_args
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert subscribed.status_code == 201
    assert subscribed_call.kwargs["is_subscription"] is True
    assert subscribed_call.kwargs["email"] == "buyer@example.com"
    assert subscribed_call.kwargs["nominal_usd"] == 10
    assert one_time.status_code == 201
    assert one_time_call.kwargs["is_subscription"] is False


async def test_verify_reads_saved_method_from_yookassa_not_from_webhook():
    api = _YooKassaApi(
        _payment_body(42, subscription=True, method_id=SAVED_METHOD_ID, saved=True)
    )
    notification = YooKassaNotification.model_validate(
        _notification(42, subscription=True, method_id="attacker-method")
    )
    with patch("app.services.payment_webhooks.httpx.AsyncClient", api.client):
        verified = await verify_yookassa_notification(notification, _settings())

    assert api.gets == 1
    assert verified.payment_method is not None
    assert verified.payment_method.id == SAVED_METHOD_ID
    assert verified.payment_method.saved is True


async def _post_webhook(db_engine, api_payload: dict, body: dict):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_db():
        async with session_maker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = _settings
    api = _YooKassaApi(api_payload)
    transport = ASGITransport(app=app)
    try:
        with (
            patch("app.services.payment_webhooks.httpx.AsyncClient", api.client),
            patch("app.services.order_fulfillment.decrypt_code", return_value="GIFT"),
            patch("app.api.v1.payment_webhook.schedule_gift_card_code_email"),
            patch("app.api.v1.payment_webhook.schedule_low_stock_alert"),
            patch(
                "app.api.v1.payment_webhook.mark_order_delivered",
                new_callable=AsyncMock,
            ),
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                response = await http.post("/api/v1/payments/webhook", json=body)
    finally:
        app.dependency_overrides.clear()
    return response


async def test_succeeded_webhook_saves_subscription_once(db_engine, db_session):
    user, order = await _seed_pending_order(db_session)
    user_id = user.id
    order_id = order.id
    api_payload = _payment_body(
        order_id,
        subscription=True,
        method_id=SAVED_METHOD_ID,
        saved=True,
    )
    body = _notification(order_id, subscription=True, method_id="attacker-method")

    first = await _post_webhook(db_engine, api_payload, body)
    second = await _post_webhook(db_engine, api_payload, body)

    assert first.status_code == 200
    assert second.status_code == 200
    db_session.expire_all()
    rows = list(await db_session.scalars(select(Subscription)))
    assert len(rows) == 1
    subscription = rows[0]
    assert subscription.user_id == user_id
    assert subscription.nominal_usd == 10
    assert subscription.payment_method_id == SAVED_METHOD_ID
    assert subscription.status is SubscriptionStatus.active
    assert subscription.dunning_attempts == 0
    assert subscription.next_billing_at - subscription.created_at == timedelta(days=30)

    fresh_order = await db_session.get(Order, order_id)
    assert fresh_order is not None
    assert fresh_order.status is OrderStatus.paid


async def test_subscription_webhook_without_saved_method_keeps_order_pending(
    db_engine, db_session
):
    _user, order = await _seed_pending_order(db_session, email="nosave@example.com")
    order_id = order.id
    api_payload = _payment_body(
        order_id,
        subscription=True,
        method_id=SAVED_METHOD_ID,
        saved=False,
    )
    body = _notification(order_id, subscription=True, method_id=None)

    response = await _post_webhook(db_engine, api_payload, body)

    assert response.status_code == 503
    db_session.expire_all()
    assert list(await db_session.scalars(select(Subscription))) == []
    fresh_order = await db_session.get(Order, order_id)
    assert fresh_order is not None
    assert fresh_order.status is OrderStatus.pending_payment


async def test_one_time_succeeded_payment_does_not_open_subscription(db_session):
    user, order = await _seed_pending_order(db_session, email="once@example.com")
    payment = VerifiedPayment.model_validate(
        _payment_body(order.id, subscription=False, method_id=SAVED_METHOD_ID, saved=True)
    )

    with patch("app.services.order_fulfillment.decrypt_code", return_value="GIFT"):
        delivery = await fulfill_verified_payment(db_session, payment)

    assert delivery is not None
    assert delivery.recipient_email == user.email
    assert list(await db_session.scalars(select(Subscription))) == []


async def test_subscription_without_saved_method_does_not_commit(db_session):
    _user, order = await _seed_pending_order(db_session, email="unsaved@example.com")
    order_id = order.id
    payment = VerifiedPayment.model_validate(
        _payment_body(order_id, subscription=True, method_id="pm_unsaved", saved=False)
    )

    with (
        patch("app.services.order_fulfillment.decrypt_code", return_value="GIFT"),
        pytest.raises(OrderFulfillmentUnavailable),
    ):
        await fulfill_verified_payment(db_session, payment)

    await db_session.rollback()
    fresh = await db_session.get(Order, order_id)
    assert fresh is not None
    assert fresh.status is OrderStatus.pending_payment
    assert list(await db_session.scalars(select(Subscription))) == []
