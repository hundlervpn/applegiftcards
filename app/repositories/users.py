from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    email = email.strip().lower()
    return await session.scalar(select(User).where(User.email == email))


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_or_create(session: AsyncSession, email: str) -> User:
    email = email.strip().lower()
    existing = await get_by_email(session, email)
    if existing is not None:
        return existing

    user = User(email=email)
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:
        existing = await get_by_email(session, email)
        if existing is None:
            raise
        return existing
    return user
