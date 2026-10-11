from sqlalchemy.ext.asyncio import AsyncSession

from app.models.price import Price


async def get_by_nominal(session: AsyncSession, nominal_usd: int) -> Price | None:
    return await session.get(Price, nominal_usd)
