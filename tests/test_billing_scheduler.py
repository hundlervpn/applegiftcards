import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.services.recurring_payments import RecurringPaymentDeclined
from app.services.scheduler import run_billing_scheduler, run_billing_tick

AS_OF = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PRICE = Decimal("1500.00")
SECRET_METHOD = "pm_secret_should_not_be_logged"


def _maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_due(setup, *, billing_at: datetime, email: str, method: str = SECRET_METHOD):
    user = User(email=email)
    setup.add(user)
    await setup.flush()
    subscription = Subscription(
        user_id=user.id,
        nominal_usd=10,
        payment_method_id=method,
        status=SubscriptionStatus.active,
        next_billing_at=billing_at,
        dunning_attempts=0,
    )
    setup.add(subscription)
    await setup.flush()
    return subscription


async def test_tick_charges_due_active_and_past_due_subscriptions_oldest_first(db_engine):
    maker = _maker(db_engine)
    async with maker() as setup:
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        user = User(email="queue@example.com")
        setup.add(user)
        await setup.flush()
        earlier = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id=SECRET_METHOD,
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF - timedelta(days=2),
        )
        same_time_first = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id="pm_same_a",
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF,
        )
        setup.add_all([earlier, same_time_first])
        await setup.flush()
        same_time_second = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id="pm_same_b",
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF,
        )
        future = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id="pm_future",
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF + timedelta(seconds=1),
        )
        past_due = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id="pm_past_due",
            status=SubscriptionStatus.past_due,
            next_billing_at=AS_OF - timedelta(days=3),
        )
        cancelled = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id="pm_cancelled",
            status=SubscriptionStatus.cancelled,
            next_billing_at=AS_OF - timedelta(days=4),
        )
        setup.add_all([same_time_second, future, past_due, cancelled])
        await setup.commit()
        expected = [past_due.id, earlier.id, same_time_first.id, same_time_second.id]

    calls: list[int] = []

    async def charge(session, subscription):
        calls.append(subscription.id)

    await run_billing_tick(maker, now=lambda: AS_OF, charge=charge)

    assert calls == expected
    assert db_engine.pool.checkedout() == 0


async def test_tick_keeps_going_when_one_subscription_fails(db_engine, caplog):
    maker = _maker(db_engine)
    async with maker() as setup:
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        declined = await _seed_due(
            setup,
            billing_at=AS_OF - timedelta(days=1),
            email="declined@example.com",
        )
        broken = await _seed_due(setup, billing_at=AS_OF, email="broken@example.com")
        healthy = await _seed_due(
            setup,
            billing_at=AS_OF,
            email="healthy@example.com",
            method="pm_healthy",
        )
        await setup.commit()
        declined_id = declined.id
        broken_id = broken.id
        healthy_id = healthy.id

    calls: list[int] = []

    async def charge(session, subscription):
        calls.append(subscription.id)
        if subscription.id == declined_id:
            raise RecurringPaymentDeclined("declined")
        if subscription.id == broken_id:
            raise RuntimeError("boom")
        subscription.next_billing_at = AS_OF + timedelta(days=30)
        await session.commit()

    caplog.set_level(logging.DEBUG)
    await run_billing_tick(maker, now=lambda: AS_OF, charge=charge)

    assert calls == [declined_id, broken_id, healthy_id]
    warning = next(
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "app.services.scheduler"
    )
    assert str(declined_id) in warning.message
    assert warning.exc_info is None
    failure = next(
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and str(broken_id) in record.message
    )
    assert failure.exc_info is not None
    assert SECRET_METHOD not in caplog.text
    assert db_engine.pool.checkedout() == 0

    async with maker() as check:
        declined_row = await check.get(Subscription, declined_id)
        healthy_row = await check.get(Subscription, healthy_id)
        assert declined_row is not None
        assert healthy_row is not None
        assert declined_row.next_billing_at == AS_OF - timedelta(days=1)
        assert healthy_row.next_billing_at == AS_OF + timedelta(days=30)


async def test_tick_charges_a_still_due_subscription_only_once(db_engine):
    maker = _maker(db_engine)
    async with maker() as setup:
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        subscription = await _seed_due(
            setup,
            billing_at=AS_OF - timedelta(days=90),
            email="overdue@example.com",
        )
        await setup.commit()
        subscription_id = subscription.id

    calls: list[int] = []

    async def charge(session, subscription):
        calls.append(subscription.id)
        subscription.next_billing_at = subscription.next_billing_at + timedelta(seconds=1)
        await session.commit()

    await run_billing_tick(maker, now=lambda: AS_OF, charge=charge)
    assert calls == [subscription_id]

    await run_billing_tick(maker, now=lambda: AS_OF, charge=charge)
    assert calls == [subscription_id, subscription_id]
    assert db_engine.pool.checkedout() == 0


async def test_later_tick_includes_subscriptions_that_become_due(db_engine):
    maker = _maker(db_engine)
    clock = {"now": AS_OF}
    async with maker() as setup:
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        due_now = await _seed_due(setup, billing_at=AS_OF, email="now@example.com")
        due_later = await _seed_due(
            setup,
            billing_at=AS_OF + timedelta(hours=1),
            email="later@example.com",
            method="pm_later",
        )
        await setup.commit()
        due_now_id = due_now.id
        due_later_id = due_later.id

    calls: list[int] = []

    async def charge(session, subscription):
        calls.append(subscription.id)
        subscription.next_billing_at = subscription.next_billing_at + timedelta(days=30)
        await session.commit()

    await run_billing_tick(maker, now=lambda: clock["now"], charge=charge)
    assert calls == [due_now_id]

    clock["now"] = AS_OF + timedelta(hours=1)
    await run_billing_tick(maker, now=lambda: clock["now"], charge=charge)
    assert calls == [due_now_id, due_later_id]


async def test_cancel_during_sleep_does_not_start_another_tick(monkeypatch):
    ticks: list[datetime] = []
    sleeps: list[float] = []
    asleep = asyncio.Event()

    async def tick(session_maker, *, now, charge):
        ticks.append(now())

    async def pause(seconds):
        sleeps.append(seconds)
        asleep.set()
        await asyncio.Future()

    monkeypatch.setattr("app.services.scheduler.run_billing_tick", tick)
    scheduler = asyncio.create_task(
        run_billing_scheduler(
            session_maker=object(),
            interval_seconds=600,
            now=lambda: AS_OF,
            sleep=pause,
        )
    )
    await asyncio.wait_for(asleep.wait(), timeout=5)
    scheduler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scheduler

    assert ticks == [AS_OF]
    assert sleeps == [600]


async def test_scheduler_uses_configured_interval(monkeypatch):
    sleeps: list[float] = []

    async def tick(session_maker, *, now, charge):
        return None

    async def pause(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr("app.services.scheduler.run_billing_tick", tick)
    with pytest.raises(asyncio.CancelledError):
        await run_billing_scheduler(session_maker=object(), sleep=pause)

    assert sleeps == [600]


async def test_failed_tick_is_logged_and_the_loop_continues(monkeypatch, caplog):
    calls = 0

    async def tick(session_maker, *, now, charge):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")

    async def pause(seconds):
        if calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.services.scheduler.run_billing_tick", tick)
    caplog.set_level(logging.ERROR, logger="app.services.scheduler")
    with pytest.raises(asyncio.CancelledError):
        await run_billing_scheduler(session_maker=object(), interval_seconds=600, sleep=pause)

    assert calls == 2
    assert "Billing tick failed" in caplog.text


async def test_cancel_during_charge_lets_that_charge_finish(db_engine):
    maker = _maker(db_engine)
    async with maker() as setup:
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        in_flight = await _seed_due(
            setup,
            billing_at=AS_OF - timedelta(days=1),
            email="shield@example.com",
        )
        waiting = await _seed_due(
            setup,
            billing_at=AS_OF,
            email="next@example.com",
            method="pm_next",
        )
        await setup.commit()
        in_flight_id = in_flight.id
        waiting_id = waiting.id

    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls: list[int] = []

    async def charge(session, subscription):
        calls.append(subscription.id)
        if subscription.id != in_flight_id:
            return
        entered.set()
        await release.wait()
        subscription.next_billing_at = AS_OF + timedelta(days=30)
        await session.commit()
        finished.set()

    tick = asyncio.create_task(
        run_billing_tick(maker, now=lambda: AS_OF, charge=charge),
        name="billing-tick-under-test",
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    tick.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
        if tick.done():
            break
    assert not finished.is_set()
    assert not tick.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tick, timeout=5)

    assert finished.is_set()
    assert calls == [in_flight_id]
    assert db_engine.pool.checkedout() == 0
    assert all(
        not task.get_name().startswith("billing-charge-")
        for task in asyncio.all_tasks()
    )
    async with maker() as check:
        stored = await check.get(Subscription, in_flight_id)
        left = await check.get(Subscription, waiting_id)
        assert stored is not None
        assert left is not None
        assert stored.next_billing_at == AS_OF + timedelta(days=30)
        assert left.next_billing_at == AS_OF


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Sequence:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.bodies: list[dict] = []

    def client(self, *args, **kwargs):
        sequence = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def post(self, url, headers=None, json=None):
                sequence.bodies.append(json)
                payload = sequence.payloads[sequence.calls]
                sequence.calls += 1
                return _Response(payload)

        return _Client()


async def test_tick_uses_charge_subscription_for_each_due_row(db_engine, monkeypatch, caplog):
    maker = _maker(db_engine)
    async with maker() as setup:
        user = User(email="real-charge@example.com")
        setup.add(user)
        setup.add(Price(nominal_usd=10, price_rub=PRICE))
        await setup.flush()
        setup.add(
            GiftCard(
                nominal_usd=10,
                code_encrypted=b"ciphertext",
                status=GiftCardStatus.available,
            )
        )
        older = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id=SECRET_METHOD,
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF - timedelta(days=1),
            dunning_attempts=2,
        )
        newer = Subscription(
            user_id=user.id,
            nominal_usd=10,
            payment_method_id=SECRET_METHOD,
            status=SubscriptionStatus.active,
            next_billing_at=AS_OF,
            dunning_attempts=2,
        )
        setup.add_all([older, newer])
        await setup.commit()
        older_id = older.id
        newer_id = newer.id

    sequence = _Sequence(
        [
            {
                "id": "pay-declined",
                "status": "canceled",
                "paid": False,
                "amount": {"value": "1500.00", "currency": "RUB"},
            },
            {
                "id": "pay-ok",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "1500.00", "currency": "RUB"},
            },
        ]
    )
    settings = Settings(
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/awe",
        yookassa_shop_id="test-shop",
        yookassa_secret_key="test-secret",
    )
    monkeypatch.setattr("app.services.recurring_payments.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payments.httpx.AsyncClient", sequence.client)
    monkeypatch.setattr("app.services.recurring_payments.decrypt_code", lambda _code: "GIFT-1")
    enqueued: list[object] = []
    dunning_notices: list[object] = []
    monkeypatch.setattr(
        "app.services.recurring_payments.enqueue_gift_card_delivery",
        enqueued.append,
    )
    monkeypatch.setattr(
        "app.services.recurring_payments.enqueue_dunning_notice",
        dunning_notices.append,
    )
    caplog.set_level(logging.DEBUG)

    await run_billing_tick(maker, now=lambda: AS_OF)

    assert sequence.calls == 2
    assert sequence.bodies[0]["metadata"]["subscription_id"] == str(older_id)
    assert sequence.bodies[1]["metadata"]["subscription_id"] == str(newer_id)
    assert len(enqueued) == 1
    assert SECRET_METHOD not in caplog.text
    assert db_engine.pool.checkedout() == 0

    async with maker() as check:
        older_row = await check.get(Subscription, older_id)
        newer_row = await check.get(Subscription, newer_id)
        orders = list((await check.scalars(select(Order))).all())
        card = await check.scalar(select(GiftCard))
        assert older_row is not None
        assert newer_row is not None
        assert older_row.status is SubscriptionStatus.cancelled
        assert older_row.next_billing_at == AS_OF - timedelta(days=1)
        assert older_row.dunning_attempts == 3
        assert len(dunning_notices) == 1
        assert dunning_notices[0].kind == "cancelled"
        assert newer_row.status is SubscriptionStatus.active
        assert newer_row.next_billing_at == AS_OF + timedelta(days=30)
        assert newer_row.dunning_attempts == 0
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.paid
        assert card is not None
        assert card.status is GiftCardStatus.sold


async def test_lifespan_starts_scheduler_and_disposes_engine_after_it_stops(monkeypatch):
    events: list[str] = []

    async def scheduler():
        events.append("started")
        try:
            await asyncio.Future()
        finally:
            events.append("stopped")

    async def dispose():
        events.append("disposed")

    monkeypatch.setattr("app.main.run_billing_scheduler", scheduler)
    monkeypatch.setattr("app.main.dispose_engine", dispose)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        running = [
            task for task in asyncio.all_tasks() if task.get_name() == "billing-scheduler"
        ]
        assert len(running) == 1
        assert events == ["started"]

    assert events == ["started", "stopped", "disposed"]
    assert all(task.get_name() != "billing-scheduler" for task in asyncio.all_tasks())
