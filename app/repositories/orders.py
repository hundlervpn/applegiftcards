from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order


async def get_for_update(session: AsyncSession, order_id: int) -> Order | None:
    return await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )


async def add(session: AsyncSession, order: Order) -> Order:
    session.add(order)
    await session.flush()
    return order
