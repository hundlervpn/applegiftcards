from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.price import Price


async def list_catalog_rows(
    session: AsyncSession,
) -> list[tuple[int, Decimal, int]]:
    available_count = func.count(GiftCard.id).label("available_count")
    statement = (
        select(Price.nominal_usd, Price.price_rub, available_count)
        .outerjoin(
            GiftCard,
            (GiftCard.nominal_usd == Price.nominal_usd)
            & (GiftCard.status == GiftCardStatus.available)
            & GiftCard.order_id.is_(None),
        )
        .group_by(Price.nominal_usd, Price.price_rub)
        .order_by(Price.nominal_usd)
    )
    result = await session.execute(statement)
    return list(result.tuples().all())
