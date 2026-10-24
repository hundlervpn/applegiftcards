from sqlalchemy import func, select
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


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    limit: int,
    offset: int,
) -> list[Order]:
    statement = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def count_for_user(session: AsyncSession, user_id: int) -> int:
    statement = select(func.count()).select_from(Order).where(Order.user_id == user_id)
    return int(await session.scalar(statement) or 0)
