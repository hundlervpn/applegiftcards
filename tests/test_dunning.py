import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

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
from app.services.dunning import (
    DUNNING_RETRY_DAYS,
    MAX_DUNNING_ATTEMPTS,
    DunningNotice,
    customer_facing_decline_reason,
    normalize_card_update_url,
    plan_dunning,
    retry_interval_phrase,
)
from app.services.email import build_dunning_email
from app.services.recurring_payments import (
    RecurringPaymentDeclined,
    charge_subscription,
    enqueue_dunning_notice,
)
from app.services.scheduler import run_billing_tick

AS_OF = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PRICE = Decimal("1500.00")
CARD_URL = "https://shop.example/account/card?x=1&y=2"
METHOD_ID = "pm_dunning_test"


def test_retry_ladder_waits_one_then_three_days_and_cancels_on_the_third_failure():
    assert DUNNING_RETRY_DAYS == (1, 3, 5)
    assert MAX_DUNNING_ATTEMPTS == 3

    first = plan_dunning(0, now=AS_OF, billing_at=AS_OF)
    assert first.status is SubscriptionStatus.past_due
    assert first.dunning_attempts == 1
    assert first.retry_days == 1
    assert first.next_billing_at == AS_OF + timedelta(days=1)

    second = plan_dunning(1, now=AS_OF + timedelta(days=1), billing_at=first.next_billing_at)
    assert second.status is SubscriptionStatus.past_due
    assert second.dunning_attempts == 2
    assert second.retry_days == 3
    assert second.next_billing_at == AS_OF + timedelta(days=4)

    # Пятые сутки опубликованы в лестнице, но третья неудача подписку закрывает.
    third = plan_dunning(2, now=second.next_billing_at, billing_at=second.next_billing_at)
    assert third.cancelled is True
    assert third.status is SubscriptionStatus.cancelled
    assert third.dunning_attempts == 3
    assert third.retry_days is None
    assert third.next_billing_at == second.next_billing_at
    assert DUNNING_RETRY_DAYS[2] == 5


def test_plan_dunning_rejects_naive_clock_and_negative_attempts():
    with pytest.raises(ValueError):
        plan_dunning(0, now=datetime(2026, 10, 23, 12, 0), billing_at=AS_OF)
    with pytest.raises(ValueError):
        plan_dunning(-1, now=AS_OF, billing_at=AS_OF)


def test_decline_reasons_and_card_link():
    assert customer_facing_decline_reason("insufficient_funds") == "Недостаточно средств на карте."
    assert "заблокирована" in customer_facing_decline_reason("payment_method_restricted")
    assert "временно" in customer_facing_decline_reason("issuer_unavailable").lower()
    assert customer_facing_decline_reason(None) == "Банк отклонил платёж."
    assert customer_facing_decline_reason("not_a_known_code") == "Банк отклонил платёж."
    assert normalize_card_update_url(CARD_URL) == CARD_URL
    assert normalize_card_update_url("javascript:alert(1)").startswith("http://")
    assert "\n" not in normalize_card_update_url("https://shop.example/card\nbcc")
    assert retry_interval_phrase(1) == "1 день"
    assert retry_interval_phrase(3) == "3 дня"
    assert retry_interval_phrase(5) == "5 дней"


def _notice(**overrides) -> DunningNotice:
    values = {
        "kind": "retry",
        "recipient_email": "buyer@example.com",
        "subscription_id": 7,
        "nominal_usd": 10,
        "reason": "Недостаточно средств на карте.",
        "card_update_url": CARD_URL,
        "attempts": 1,
        "retry_days": 1,
        "next_billing_at": AS_OF + timedelta(days=1),
    }
    values.update(overrides)
    return DunningNotice(**values)


def test_warning_email_contains_reason_and_card_update_link():
    message = build_dunning_email(_notice(), from_email="no-reply@okno.example")
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()

    assert message["To"] == "buyer@example.com"
    assert message["Subject"] == "Не удалось списать оплату подписки №7"
    assert "Недостаточно средств на карте." in plain
    assert CARD_URL in plain
    assert "1 день" in plain
    assert "Обновить карту" in html
    assert "Недостаточно средств на карте." in html
    assert "shop.example/account/card" in html
    assert "автодоставки" in plain.lower() or "автосписание" in plain.lower()


def test_cancellation_email_announces_the_end_of_autodelivery():
    message = build_dunning_email(
        _notice(
            kind="cancelled",
            attempts=3,
            retry_days=None,
            next_billing_at=None,
            reason="Карта заблокирована или банк ограничил платежи по ней.",
            card_update_url="https://shop.example/should-not-appear",
        ),
        from_email="no-reply@okno.example",
    )
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()

    assert "аннулирована" in message["Subject"]
    assert "Автодоставка аннулирована" in plain
    assert "не будут" in plain
    assert "Карта заблокирована" in plain
    assert "should-not-appear" not in plain
    assert "should-not-appear" not in html
    assert "Обновить карту" not in html
    assert "аннулирована" in html


def test_dunning_email_escapes_reason():
    message = build_dunning_email(
        _notice(reason='<script>alert("x")</script>'),
        from_email="no-reply@okno.example",
    )
    html = message.get_body(("html",)).get_content()
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/awe",
        yookassa_shop_id="test-shop",
        yookassa_secret_key="test-secret",
        card_update_url=CARD_URL,
    )


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Gateway:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.headers: list[dict] = []

    def client(self, *args, **kwargs):
        gateway = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def post(self, url, headers=None, json=None):
                gateway.calls += 1
                gateway.headers.append(dict(headers or {}))
                return _Response(gateway.payload)

        return _Client()


def _refused(reason: str) -> dict:
    return {
        "id": "pay-declined",
        "status": "canceled",
        "paid": False,
        "amount": {"value": "1500.00", "currency": "RUB"},
        "cancellation_details": {"party": "payment_network", "reason": reason},
    }


def _succeeded() -> dict:
    return {
        "id": "pay-ok",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "1500.00", "currency": "RUB"},
    }


async def _seed(session, *, status=SubscriptionStatus.active, attempts=0, billing_at=AS_OF):
    user = User(email=f"dunning-{attempts}-{status.value}@example.com")
    session.add(user)
    session.add(Price(nominal_usd=10, price_rub=PRICE))
    await session.flush()
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
        dunning_attempts=attempts,
    )
    session.add(subscription)
    await session.commit()
    return subscription


@pytest.mark.parametrize(
    ("reason_code", "reason_text"),
    [
        ("insufficient_funds", "Недостаточно средств на карте."),
        ("payment_method_restricted", "Карта заблокирована или банк ограничил платежи по ней."),
        ("issuer_unavailable", "Банк временно недоступен."),
    ],
)
async def test_decline_marks_past_due_and_emails_the_reason(db_session, monkeypatch, reason_code, reason_text):
    subscription = await _seed(db_session)
    notices: list[DunningNotice] = []
    monkeypatch.setattr("app.services.recurring_payments.get_settings", _settings)
    monkeypatch.setattr("app.services.recurring_payments.decrypt_code", lambda _code: "GIFT-1")
    monkeypatch.setattr("app.services.recurring_payments.utcnow", lambda: AS_OF)
    monkeypatch.setattr("app.services.payments.httpx.AsyncClient", _Gateway(_refused(reason_code)).client)
    monkeypatch.setattr("app.services.recurring_payments.enqueue_dunning_notice", notices.append)
    monkeypatch.setattr("app.services.recurring_payments.enqueue_gift_card_delivery", lambda _delivery: None)

    with pytest.raises(RecurringPaymentDeclined):
        await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.past_due
    assert subscription.dunning_attempts == 1
    assert subscription.next_billing_at == AS_OF + timedelta(days=1)
    assert list((await db_session.scalars(select(Order))).all()) == []
    card = await db_session.scalar(select(GiftCard))
    assert card is not None
    assert card.status is GiftCardStatus.available
    assert len(notices) == 1
    assert notices[0].kind == "retry"
    assert notices[0].reason == reason_text
    assert notices[0].card_update_url == CARD_URL
    assert notices[0].retry_days == 1
    assert METHOD_ID not in notices[0].reason


async def test_three_failures_cancel_subscription_and_remove_it_from_billing(db_engine, monkeypatch):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as setup:
        subscription = await _seed(setup)
        subscription_id = subscription.id

    gateway = _Gateway(_refused("insufficient_funds"))
    notices: list[DunningNotice] = []
    clock = {"now": AS_OF}
    monkeypatch.setattr("app.services.recurring_payments.get_settings", _settings)
    monkeypatch.setattr("app.services.recurring_payments.decrypt_code", lambda _code: "GIFT-1")
    monkeypatch.setattr("app.services.recurring_payments.utcnow", lambda: clock["now"])
    monkeypatch.setattr("app.services.payments.httpx.AsyncClient", gateway.client)
    monkeypatch.setattr("app.services.recurring_payments.enqueue_dunning_notice", notices.append)

    await run_billing_tick(maker, now=lambda: clock["now"])
    clock["now"] = AS_OF + timedelta(days=1)
    await run_billing_tick(maker, now=lambda: clock["now"])
    clock["now"] = AS_OF + timedelta(days=4)
    await run_billing_tick(maker, now=lambda: clock["now"])

    async with maker() as check:
        stored = await check.get(Subscription, subscription_id)
        assert stored is not None
        assert stored.status is SubscriptionStatus.cancelled
        assert stored.dunning_attempts == 3
        assert stored.next_billing_at == AS_OF + timedelta(days=4)
        assert list((await check.scalars(select(Order))).all()) == []
        card = await check.scalar(select(GiftCard))
        assert card is not None
        assert card.status is GiftCardStatus.available

    assert [notice.kind for notice in notices] == ["retry", "retry", "cancelled"]
    assert [notice.retry_days for notice in notices] == [1, 3, None]
    assert gateway.calls == 3
    keys = [headers["Idempotence-Key"] for headers in gateway.headers]
    assert len(set(keys)) == 3

    clock["now"] = AS_OF + timedelta(days=40)
    await run_billing_tick(maker, now=lambda: clock["now"])
    assert gateway.calls == 3


async def test_successful_retry_resumes_active_subscription(db_session, monkeypatch):
    subscription = await _seed(
        db_session,
        status=SubscriptionStatus.past_due,
        attempts=2,
    )
    notices: list[DunningNotice] = []
    deliveries: list[object] = []
    monkeypatch.setattr("app.services.recurring_payments.get_settings", _settings)
    monkeypatch.setattr("app.services.recurring_payments.decrypt_code", lambda _code: "GIFT-1")
    monkeypatch.setattr("app.services.payments.httpx.AsyncClient", _Gateway(_succeeded()).client)
    monkeypatch.setattr("app.services.recurring_payments.enqueue_dunning_notice", notices.append)
    monkeypatch.setattr("app.services.recurring_payments.enqueue_gift_card_delivery", deliveries.append)

    result = await charge_subscription(db_session, subscription)

    await db_session.refresh(subscription)
    assert result.external_payment_id == "pay-ok"
    assert subscription.status is SubscriptionStatus.active
    assert subscription.dunning_attempts == 0
    assert subscription.next_billing_at == AS_OF + timedelta(days=30)
    orders = list((await db_session.scalars(select(Order))).all())
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.paid
    card = await db_session.scalar(select(GiftCard))
    assert card is not None
    assert card.status is GiftCardStatus.sold
    assert notices == []
    assert len(deliveries) == 1


async def test_dunning_email_failure_does_not_escape_the_background_task():
    notice = _notice()
    loop = asyncio.get_running_loop()
    created: list[asyncio.Task] = []
    original = loop.create_task

    def spy(coro, *, name=None):
        task = original(coro, name=name)
        created.append(task)
        return task

    with (
        patch.object(loop, "create_task", spy),
        patch(
            "app.services.recurring_payments.send_dunning_notice",
            new=AsyncMock(side_effect=RuntimeError("smtp down")),
        ),
    ):
        enqueue_dunning_notice(notice)

    assert len(created) == 1
    assert created[0].get_name() == "dunning-email-7"
    await created[0]
    assert created[0].exception() is None
