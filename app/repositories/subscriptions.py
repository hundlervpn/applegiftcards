from collections.abc import Collection
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SubscriptionStatus
from app.models.price import Price
from app.models.subscription import Subscription


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, subscription: Subscription) -> Subscription:
        self._session.add(subscription)
        await self._session.flush()
        return subscription

    async def get_by_id(self, subscription_id: int) -> Subscription | None:
        return await self._session.get(Subscription, subscription_id)

    async def get_for_update(self, subscription_id: int) -> Subscription | None:
        statement = (
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def list_for_user_with_prices(self, user_id: int) -> list[tuple[Subscription, Decimal]]:
        statement = (
            select(Subscription, Price.price_rub)
            .join(Price, Price.nominal_usd == Subscription.nominal_usd)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
        rows = await self._session.execute(statement)
        return [(subscription, price_rub) for subscription, price_rub in rows]

    async def list_active_due_for_billing(
        self,
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
        return list(await self._session.scalars(statement))

    async def lock_next_due_for_billing(
        self,
        *,
        as_of: datetime | None = None,
        exclude_ids: Collection[int] | None = None,
    ) -> Subscription | None:
        """Lock the oldest active due row, skipping rows held by other transactions."""
        moment = datetime.now(timezone.utc) if as_of is None else as_of
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError("as_of must be timezone-aware")

        filters = [
            Subscription.status == SubscriptionStatus.active,
            Subscription.next_billing_at <= moment,
        ]
        if exclude_ids:
            filters.append(Subscription.id.not_in(tuple(exclude_ids)))
        statement = (
            select(Subscription)
            .where(*filters)
            .order_by(Subscription.next_billing_at, Subscription.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return await self._session.scalar(statement)

    async def update_status(
        self,
        subscription_id: int,
        status: SubscriptionStatus,
    ) -> Subscription | None:
        normalized = SubscriptionStatus(status)
        subscription = await self.get_by_id(subscription_id)
        if subscription is None:
            return None
        subscription.status = normalized
        await self._session.flush()
        return subscription
