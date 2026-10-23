from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SubscriptionStatus
from app.models.subscription import Subscription


async def add(session: AsyncSession, subscription: Subscription) -> Subscription:
    session.add(subscription)
    await session.flush()
    return subscription


async def get_by_id(session: AsyncSession, subscription_id: int) -> Subscription | None:
    return await session.get(Subscription, subscription_id)


async def list_active_due_for_billing(
    session: AsyncSession,
    *,
    as_of: datetime | None = None,
) -> list[Subscription]:
    """Active subscriptions with next_billing_at at or before as_of, oldest first."""
    moment = datetime.now(timezone.utc) if as_of is None else as_of
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("as_of must be timezone-aware")

    statement = (
        select(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.next_billing_at <= moment,
        )
        .order_by(Subscription.next_billing_at, Subscription.id)
    )
    return list(await session.scalars(statement))


async def update_status(
    session: AsyncSession,
    subscription_id: int,
    status: SubscriptionStatus,
) -> Subscription | None:
    normalized = SubscriptionStatus(status)
    subscription = await get_by_id(session, subscription_id)
    if subscription is None:
        return None
    subscription.status = normalized
    await session.flush()
    return subscription
