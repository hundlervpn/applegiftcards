import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.models.enums import SubscriptionStatus
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.repositories.subscriptions import SubscriptionRepository
from app.services import tokens
from app.services.email import (
    EmailConfigurationError,
    SubscriptionCancelledNotice,
    build_subscription_cancelled_email,
    send_subscription_cancelled_email,
)
from app.services.recurring_payments import RecurringChargeNotAllowed, charge_subscription
from app.services.scheduler import run_billing_tick
from app.services.subscription_cancellation import cancel_subscription

URL = "/api/v1/subscriptions/{subscription_id}/cancel"
JWT_SECRET = "scrum-39-test-signing-secret-at-least-32-bytes"
CANCELLED_AT = datetime(2026, 10, 25, 16, 32, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
DUE_AT = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
METHOD_ID = "pm_owner_secret"


@pytest.fixture(autouse=True)
def cancellation_settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def frozen_cancellation_clock(monkeypatch):
    monkeypatch.setattr(
        "app.services.subscription_cancellation._utc_now",
        lambda: CANCELLED_AT,
    )


def headers(user_id):
    return {"Authorization": f"Bearer {tokens.create_access_token(user_id)}"}


def cancel_url(subscription_id):
    return URL.format(subscription_id=subscription_id)


async def add_user(session, email="owner@example.com"):
    user = User(email=email)
    session.add(user)
    await session.commit()
    return user


async def add_price(session, nominal_usd=10):
    session.add(Price(nominal_usd=nominal_usd, price_rub=Decimal("1500.00")))
    await session.commit()


async def add_subscription(
    session,
    user_id,
    *,
    status=SubscriptionStatus.active,
    method_id=METHOD_ID,
    updated_at=CREATED_AT,
    next_billing_at=DUE_AT,
    dunning_attempts=2,
):
    subscription = Subscription(
        user_id=user_id,
        nominal_usd=10,
        payment_method_id=method_id,
        status=status,
        next_billing_at=next_billing_at,
        dunning_attempts=dunning_attempts,
        created_at=CREATED_AT,
        updated_at=updated_at,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def wait_for_cancellation_emails():
    await asyncio.sleep(0)
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("subscription-cancelled-email-")
    ]
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("authorization", [None, "Bearer garbage", "Bearer"])
async def test_requires_access_token(client, authorization):
    response = await client.post(
        cancel_url(1),
        headers={} if authorization is None else {"Authorization": authorization},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("subscription_id", [0, -1, 2**63])
async def test_rejects_id_outside_bigint(client, db_session, subscription_id):
    user = await add_user(db_session)
    response = await client.post(cancel_url(subscription_id), headers=headers(user.id))
    assert response.status_code == 422


async def test_missing_subscription_is_not_found(client, db_session):
    user = await add_user(db_session)
    response = await client.post(cancel_url(999_999), headers=headers(user.id))
    assert response.status_code == 404
    assert response.json()["detail"] == "Subscription not found"


async def test_other_users_subscription_is_forbidden(client, db_session, caplog):
    owner = await add_user(db_session)
    stranger = await add_user(db_session, "stranger@example.com")
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id)
    send = AsyncMock()
    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        send,
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(stranger.id))
        await wait_for_cancellation_emails()

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
    assert METHOD_ID not in response.text
    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.active
    assert subscription.updated_at == CREATED_AT
    send.assert_not_awaited()
    assert METHOD_ID not in caplog.text


@pytest.mark.parametrize("status", [SubscriptionStatus.active, SubscriptionStatus.past_due])
async def test_owner_cancels_and_scheduler_skips_the_row(
    client,
    db_engine,
    db_session,
    caplog,
    status,
):
    owner = await add_user(db_session, f"{status.value}@example.com")
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id, status=status)
    sibling = await add_subscription(
        db_session,
        owner.id,
        method_id="pm_sibling",
        next_billing_at=DUE_AT - timedelta(days=1),
        dunning_attempts=0,
    )
    send = AsyncMock()
    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        send,
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "status", "updated_at"}
    assert body["id"] == subscription.id
    assert body["status"] == "cancelled"
    assert datetime.fromisoformat(body["updated_at"]) == CANCELLED_AT
    assert METHOD_ID not in response.text
    assert METHOD_ID not in caplog.text
    send.assert_awaited_once()
    notice = send.await_args.args[0]
    assert notice.recipient_email == owner.email
    assert notice.subscription_id == subscription.id
    assert notice.nominal_usd == 10
    assert notice.cancelled_at == CANCELLED_AT
    assert not hasattr(notice, "payment_method_id")

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.cancelled
    assert subscription.updated_at == CANCELLED_AT
    assert subscription.next_billing_at == DUE_AT
    assert subscription.dunning_attempts == 2
    assert subscription.payment_method_id == METHOD_ID

    charged = []

    async def charge(_session, row):
        charged.append(row.id)

    await run_billing_tick(
        async_sessionmaker(db_engine, expire_on_commit=False),
        now=lambda: DUE_AT,
        charge=charge,
    )
    assert charged == [sibling.id]


async def test_repeated_cancel_keeps_timestamp_and_sends_one_email(client, db_session):
    owner = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id)
    send = AsyncMock()
    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        send,
    ):
        first = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()
        second = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["status"] == "cancelled"
    send.assert_awaited_once()
    await db_session.refresh(subscription)
    assert subscription.updated_at == CANCELLED_AT


async def test_already_cancelled_subscription_does_not_send_email(client, db_session):
    owner = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(
        db_session,
        owner.id,
        status=SubscriptionStatus.cancelled,
        updated_at=CREATED_AT,
    )
    send = AsyncMock()
    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        send,
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert datetime.fromisoformat(response.json()["updated_at"]) == CREATED_AT
    send.assert_not_awaited()
    await db_session.refresh(subscription)
    assert subscription.updated_at == CREATED_AT


async def test_email_enqueue_failure_still_cancels(client, db_session):
    owner = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id)
    with patch(
        "app.services.subscription_cancellation.enqueue_subscription_cancelled_email",
        side_effect=RuntimeError("loop is closed"),
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(owner.id))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.cancelled


async def test_smtp_failure_does_not_restore_the_subscription(client, db_session):
    owner = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id)

    async def fail(_notice):
        raise EmailConfigurationError("SMTP_USER and SMTP_PASSWORD must be configured")

    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        fail,
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()

    assert response.status_code == 200
    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.cancelled
    assert subscription.updated_at == CANCELLED_AT


async def test_cancelled_subscription_is_not_sent_to_the_acquirer(client, db_engine, db_session):
    owner = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, owner.id)
    with patch(
        "app.services.subscription_cancellation.send_subscription_cancelled_email",
        AsyncMock(),
    ):
        response = await client.post(cancel_url(subscription.id), headers=headers(owner.id))
        await wait_for_cancellation_emails()
    assert response.status_code == 200

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as worker:
        loaded = await worker.get(Subscription, subscription.id)
        with patch(
            "app.services.recurring_payments.create_recurring_payment",
            AsyncMock(side_effect=AssertionError("acquirer must not be called")),
        ):
            with pytest.raises(RecurringChargeNotAllowed):
                await charge_subscription(worker, loaded)


async def test_cancel_waits_until_the_billing_lock_is_released(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await add_user(setup, "locked-owner@example.com")
        await add_price(setup)
        subscription = await add_subscription(setup, owner.id, method_id="pm_locked")
        subscription_id = subscription.id
        user_id = owner.id

    holder = maker()
    other = maker()
    probe = maker()
    send = AsyncMock()
    task = None
    try:
        held = await SubscriptionRepository(holder).get_for_update(subscription_id)
        assert held is not None
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        with patch(
            "app.services.subscription_cancellation.send_subscription_cancelled_email",
            send,
        ):
            task = asyncio.create_task(
                cancel_subscription(
                    other,
                    subscription_id=subscription_id,
                    user_id=user_id,
                )
            )
            await _wait_until_cancel_blocks(probe, task, holder_pid)
            assert not task.done()
            await holder.rollback()
            result = await asyncio.wait_for(task, timeout=5)
            await wait_for_cancellation_emails()

        assert result.status is SubscriptionStatus.cancelled
        assert result.updated_at == CANCELLED_AT
        send.assert_awaited_once()
        async with maker() as check:
            stored = await check.get(Subscription, subscription_id)
            assert stored.status is SubscriptionStatus.cancelled
    finally:
        await holder.rollback()
        await holder.close()
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (Exception, asyncio.CancelledError):
                pass
        await other.rollback()
        await other.close()
        await probe.rollback()
        await probe.close()


async def _wait_until_cancel_blocks(probe, task, holder_pid):
    """Ждёт, пока отмена встанет в очередь за транзакцией, которая держит строку."""
    query = text(
        """
        SELECT count(*) FROM pg_stat_activity
        WHERE wait_event_type = 'Lock'
          AND :holder = ANY (pg_blocking_pids(pid))
        """
    )
    for _ in range(50):
        waiting = await probe.scalar(query, {"holder": holder_pid})
        if waiting:
            return
        if task.done():
            raise AssertionError(f"cancel finished instead of waiting: {task.exception()!r}")
        await asyncio.sleep(0.05)
    raise AssertionError("cancel did not wait on the subscription row lock")


def test_cancellation_email_confirms_the_stop_and_escapes_html():
    notice = SubscriptionCancelledNotice(
        subscription_id=42,
        recipient_email="owner<script>@example.com",
        nominal_usd=10,
        cancelled_at=CANCELLED_AT,
    )
    message = build_subscription_cancelled_email(notice, from_email="no-reply@okno.example")
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()

    assert "owner" in message["To"]
    assert "script" in message["To"]
    assert message["Subject"] == "Автопродление подписки №42 отменено"
    assert "выполняться не будут" in plain
    assert "25.10.2026 16:32 UTC" in plain
    assert "$10" in plain
    assert "pm_" not in plain
    assert "pm_" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "25.10.2026 16:32 UTC" in html


def test_cancellation_email_rejects_naive_timestamp():
    notice = SubscriptionCancelledNotice(
        subscription_id=1,
        recipient_email="owner@example.com",
        nominal_usd=10,
        cancelled_at=datetime(2026, 10, 25, 16, 32),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_subscription_cancelled_email(notice, from_email="no-reply@okno.example")


async def test_send_cancellation_email_hands_message_to_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "no-reply@okno.example")
    get_settings.cache_clear()
    captured = {}

    async def capture(message, **_kwargs):
        captured["message"] = message

    monkeypatch.setattr("app.services.email.aiosmtplib.send", capture)
    notice = SubscriptionCancelledNotice(
        subscription_id=7,
        recipient_email="owner@example.com",
        nominal_usd=25,
        cancelled_at=CANCELLED_AT,
    )
    await send_subscription_cancelled_email(notice)

    assert captured["message"]["To"] == "owner@example.com"
    assert "№7" in captured["message"]["Subject"]
    assert "$25" in captured["message"].get_body(("plain",)).get_content()


async def test_openapi_documents_owner_cancel(client):
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/api/v1/subscriptions/{subscription_id}/cancel"]["post"]
    assert operation["tags"] == ["subscriptions"]
    assert {"200", "401", "403", "404"} <= set(operation["responses"])
    example = operation["responses"]["200"]["content"]["application/json"]["examples"]["cancelled"]["value"]
    assert example["status"] == "cancelled"
