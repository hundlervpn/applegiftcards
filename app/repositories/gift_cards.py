from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.price import Price


async def add_batch(
    session: AsyncSession,
    cards: list[GiftCard],
) -> int:
    try:
        session.add_all(cards)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        raise
    return len(cards)


async def count_by_status(session: AsyncSession) -> list[tuple[int, int, int, int]]:
    statement = (
        select(
            Price.nominal_usd,
            func.count(GiftCard.id).filter(GiftCard.status == GiftCardStatus.available),
            func.count(GiftCard.id).filter(GiftCard.status == GiftCardStatus.reserved),
            func.count(GiftCard.id).filter(GiftCard.status == GiftCardStatus.sold),
        )
        .outerjoin(GiftCard, GiftCard.nominal_usd == Price.nominal_usd)
        .group_by(Price.nominal_usd)
        .order_by(Price.nominal_usd)
    )
    result = await session.execute(statement)
    return [tuple(row) for row in result]
