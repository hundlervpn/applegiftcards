from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.order import Order


async def lock_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )


async def lock_reserved_cards_for_order(
    session: AsyncSession,
    order_id: int,
) -> list[GiftCard]:
    statement = (
        select(GiftCard)
        .where(
            GiftCard.order_id == order_id,
            GiftCard.status == GiftCardStatus.reserved,
        )
        .order_by(GiftCard.id)
        .with_for_update()
    )
    return list((await session.scalars(statement)).all())


async def lock_available_cards(
    session: AsyncSession,
    nominal_usd: int,
    quantity: int,
) -> list[GiftCard]:
    statement = (
        select(GiftCard)
        .where(
            GiftCard.nominal_usd == nominal_usd,
            GiftCard.status == GiftCardStatus.available,
            GiftCard.order_id.is_(None),
        )
        .order_by(GiftCard.id)
        .limit(quantity)
        .with_for_update(skip_locked=True)
    )
    return list((await session.scalars(statement)).all())


async def list_sold_for_orders(
    session: AsyncSession,
    order_ids: list[int],
) -> list[GiftCard]:
    statement = (
        select(GiftCard)
        .where(
            GiftCard.order_id.in_(order_ids),
            GiftCard.status == GiftCardStatus.sold,
        )
        .order_by(GiftCard.order_id, GiftCard.id)
    )
    return list((await session.scalars(statement)).all())
