from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.support_ticket import SupportTicket


async def owns_order(session: AsyncSession, user_id: int, order_id: int) -> bool:
    statement = select(Order.id).where(Order.id == order_id, Order.user_id == user_id)
    return await session.scalar(statement) is not None


async def add(session: AsyncSession, ticket: SupportTicket) -> SupportTicket:
    session.add(ticket)
    await session.flush()
    return ticket


async def list_for_user(
    session: AsyncSession, user_id: int, limit: int, offset: int
) -> list[SupportTicket]:
    statement = (
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id)
        .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(statement))


async def count_for_user(session: AsyncSession, user_id: int) -> int:
    statement = (
        select(func.count()).select_from(SupportTicket).where(SupportTicket.user_id == user_id)
    )
    return int(await session.scalar(statement) or 0)
