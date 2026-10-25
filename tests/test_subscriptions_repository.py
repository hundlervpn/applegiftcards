import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import SubscriptionStatus
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.repositories.subscriptions import SubscriptionRepository

AS_OF = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def subscriptions(db_session) -> SubscriptionRepository:
    return SubscriptionRepository(db_session)


async def _user(session, email: str = "subscriber@example.com") -> User:
    user = User(email=email)
    session.add(user)
    await session.flush()
    return user


async def _price(session, nominal_usd: int = 10) -> Price:
    price = Price(nominal_usd=nominal_usd, price_rub=Decimal("1500.00"))
    session.add(price)
    await session.flush()
    return price


def _subscription(
    user_id: int,
    *,
    nominal_usd: int = 10,
    payment_method_id: str = "pm_123",
    status: SubscriptionStatus = SubscriptionStatus.active,
    next_billing_at: datetime = AS_OF,
    dunning_attempts: int = 0,
) -> Subscription:
    return Subscription(
        user_id=user_id,
        nominal_usd=nominal_usd,
        payment_method_id=payment_method_id,
        status=status,
        next_billing_at=next_billing_at,
        dunning_attempts=dunning_attempts,
    )


async def test_add_and_get_by_id_round_trip(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)

    stored = await subscriptions.add(_subscription(user.id))

    assert stored.id is not None
    assert stored.created_at is not None
    assert stored.created_at.tzinfo is not None
    assert stored.updated_at is not None
    assert stored.updated_at.tzinfo is not None

    loaded = await subscriptions.get_by_id(stored.id)
    assert loaded is not None
    assert loaded.user_id == user.id
    assert loaded.nominal_usd == 10
    assert loaded.payment_method_id == "pm_123"
    assert loaded.status is SubscriptionStatus.active
    assert loaded.next_billing_at == AS_OF
    assert loaded.dunning_attempts == 0


async def test_get_by_id_returns_none_when_missing(subscriptions):
    assert await subscriptions.get_by_id(999_999) is None


async def test_add_persists_database_defaults_when_status_and_dunning_omitted(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)
    subscription = Subscription(
        user_id=user.id,
        nominal_usd=10,
        payment_method_id="pm_defaults",
        next_billing_at=AS_OF,
    )

    stored = await subscriptions.add(subscription)

    assert stored.status is SubscriptionStatus.active
    assert stored.dunning_attempts == 0


async def test_list_active_due_for_billing_filters_and_orders(db_session, subscriptions):
    first_user = await _user(db_session, "first@example.com")
    second_user = await _user(db_session, "second@example.com")
    await _price(db_session, 10)
    await _price(db_session, 25)

    earlier = await subscriptions.add(
        _subscription(first_user.id, next_billing_at=AS_OF - timedelta(days=2)),
    )
    exact = await subscriptions.add(
        _subscription(
            second_user.id,
            nominal_usd=25,
            next_billing_at=AS_OF,
            payment_method_id="pm_exact",
        ),
    )
    same_time_later_id = await subscriptions.add(
        _subscription(
            first_user.id,
            next_billing_at=AS_OF,
            payment_method_id="pm_same_time",
        ),
    )
    await subscriptions.add(
        _subscription(first_user.id, next_billing_at=AS_OF + timedelta(seconds=1)),
    )
    await subscriptions.add(
        _subscription(
            second_user.id,
            nominal_usd=25,
            status=SubscriptionStatus.past_due,
            next_billing_at=AS_OF - timedelta(days=1),
            payment_method_id="pm_past_due",
        ),
    )
    await subscriptions.add(
        _subscription(
            second_user.id,
            nominal_usd=25,
            status=SubscriptionStatus.cancelled,
            next_billing_at=AS_OF - timedelta(days=3),
            payment_method_id="pm_cancelled",
        ),
    )

    due = await subscriptions.list_active_due_for_billing(as_of=AS_OF)

    assert [row.id for row in due] == [earlier.id, exact.id, same_time_later_id.id]


async def test_list_active_due_for_billing_defaults_to_current_time(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)
    now = datetime.now(timezone.utc)
    due = await subscriptions.add(
        _subscription(user.id, next_billing_at=now - timedelta(days=1)),
    )
    await subscriptions.add(
        _subscription(
            user.id,
            next_billing_at=now + timedelta(days=365),
            payment_method_id="pm_future",
        ),
    )

    rows = await subscriptions.list_active_due_for_billing()

    assert [row.id for row in rows] == [due.id]


async def test_list_active_due_for_billing_rejects_naive_datetime(subscriptions):
    with pytest.raises(ValueError, match="timezone-aware"):
        await subscriptions.list_active_due_for_billing(
            as_of=datetime(2026, 10, 23, 12, 0),
        )


async def test_update_status_persists_and_drops_row_from_due_list(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)
    stored = await subscriptions.add(_subscription(user.id))

    updated = await subscriptions.update_status(
        stored.id,
        SubscriptionStatus.past_due,
    )

    assert updated is not None
    assert updated.status is SubscriptionStatus.past_due
    loaded = await subscriptions.get_by_id(stored.id)
    assert loaded is not None
    assert loaded.status is SubscriptionStatus.past_due
    assert await subscriptions.list_active_due_for_billing(as_of=AS_OF) == []

    cancelled = await subscriptions.update_status(stored.id, "cancelled")
    assert cancelled is not None
    assert cancelled.status is SubscriptionStatus.cancelled


async def test_update_status_returns_none_for_missing_row(subscriptions):
    assert await subscriptions.update_status(999_999, SubscriptionStatus.active) is None


async def test_update_status_rejects_unknown_value(subscriptions):
    with pytest.raises(ValueError):
        await subscriptions.update_status(1, "paused")


async def test_add_rejects_unknown_user_and_nominal(db_session, subscriptions):
    await _price(db_session)
    user = await _user(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await subscriptions.add(_subscription(user_id=999_999))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await subscriptions.add(_subscription(user.id, nominal_usd=15))


async def test_add_rejects_negative_dunning_and_blank_payment_method(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await subscriptions.add(_subscription(user.id, dunning_attempts=-1))

    for payment_method_id in ("", "   "):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await subscriptions.add(
                    _subscription(user.id, payment_method_id=payment_method_id),
                )


async def test_lock_next_due_for_billing_includes_past_due_and_skips_cancelled(
    db_session,
    subscriptions,
):
    user = await _user(db_session)
    await _price(db_session, 10)
    await _price(db_session, 25)
    earliest = await subscriptions.add(
        _subscription(user.id, next_billing_at=AS_OF - timedelta(days=2)),
    )
    await subscriptions.add(
        _subscription(
            user.id,
            nominal_usd=25,
            next_billing_at=AS_OF,
            payment_method_id="pm_later",
        ),
    )
    past_due = await subscriptions.add(
        _subscription(
            user.id,
            status=SubscriptionStatus.past_due,
            next_billing_at=AS_OF - timedelta(days=3),
            payment_method_id="pm_past_due",
        ),
    )
    await subscriptions.add(
        _subscription(
            user.id,
            status=SubscriptionStatus.cancelled,
            next_billing_at=AS_OF - timedelta(days=4),
            payment_method_id="pm_cancelled",
        ),
    )
    await subscriptions.add(
        _subscription(
            user.id,
            next_billing_at=AS_OF + timedelta(seconds=1),
            payment_method_id="pm_future",
        ),
    )

    locked = await subscriptions.lock_next_due_for_billing(as_of=AS_OF)

    assert locked is not None
    assert locked.id == past_due.id
    assert locked.status is SubscriptionStatus.past_due

    # Отменённая строка старше, но в биллинг не попадает.
    nxt = await subscriptions.lock_next_due_for_billing(
        as_of=AS_OF,
        exclude_ids=[past_due.id],
    )
    assert nxt is not None
    assert nxt.id == earliest.id


async def test_lock_next_due_for_billing_skips_excluded_ids(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)
    first = await subscriptions.add(
        _subscription(user.id, next_billing_at=AS_OF - timedelta(days=1)),
    )
    second = await subscriptions.add(
        _subscription(
            user.id,
            next_billing_at=AS_OF,
            payment_method_id="pm_second",
        ),
    )

    locked = await subscriptions.lock_next_due_for_billing(
        as_of=AS_OF,
        exclude_ids=[first.id],
    )

    assert locked is not None
    assert locked.id == second.id


async def test_lock_next_due_for_billing_rejects_naive_datetime(subscriptions):
    with pytest.raises(ValueError, match="timezone-aware"):
        await subscriptions.lock_next_due_for_billing(
            as_of=datetime(2026, 10, 23, 12, 0),
        )


async def test_lock_next_due_for_billing_skips_row_held_by_another_transaction(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as setup:
        user = await _user(setup, "locked@example.com")
        await _price(setup)
        earlier = await SubscriptionRepository(setup).add(
            _subscription(user.id, next_billing_at=AS_OF - timedelta(days=1)),
        )
        later = await SubscriptionRepository(setup).add(
            _subscription(
                user.id,
                next_billing_at=AS_OF,
                payment_method_id="pm_later",
            ),
        )
        await setup.commit()
        earlier_id = earlier.id
        later_id = later.id

    holder = maker()
    other = maker()
    try:
        held = await SubscriptionRepository(holder).lock_next_due_for_billing(as_of=AS_OF)
        assert held is not None
        assert held.id == earlier_id

        skipped = await asyncio.wait_for(
            SubscriptionRepository(other).lock_next_due_for_billing(as_of=AS_OF),
            timeout=5,
        )
        assert skipped is not None
        assert skipped.id == later_id

        visible = await asyncio.wait_for(
            SubscriptionRepository(other).list_active_due_for_billing(as_of=AS_OF),
            timeout=5,
        )
        assert [row.id for row in visible] == [earlier_id, later_id]
    finally:
        await holder.rollback()
        await holder.close()
        await other.rollback()
        await other.close()


async def test_lock_next_due_for_billing_returns_none_when_the_only_row_is_locked(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as setup:
        user = await _user(setup, "only-locked@example.com")
        await _price(setup)
        stored = await SubscriptionRepository(setup).add(_subscription(user.id))
        await setup.commit()
        stored_id = stored.id

    holder = maker()
    other = maker()
    try:
        held = await SubscriptionRepository(holder).lock_next_due_for_billing(as_of=AS_OF)
        assert held is not None
        assert held.id == stored_id
        skipped = await asyncio.wait_for(
            SubscriptionRepository(other).lock_next_due_for_billing(as_of=AS_OF),
            timeout=5,
        )
        assert skipped is None
    finally:
        await holder.rollback()
        await holder.close()
        await other.rollback()
        await other.close()


async def test_add_rejects_payment_method_longer_than_64(db_session, subscriptions):
    user = await _user(db_session)
    await _price(db_session)
    accepted = await subscriptions.add(
        _subscription(user.id, payment_method_id="p" * 64),
    )
    assert len(accepted.payment_method_id) == 64

    with pytest.raises(DBAPIError, match="value too long"):
        async with db_session.begin_nested():
            await subscriptions.add(
                _subscription(user.id, payment_method_id="p" * 65),
            )
