import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories import users as users_repo
from app.services import tokens
from app.services.security import hash_password, verify_password

logger = logging.getLogger(__name__)


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


async def register_user(
    session: AsyncSession,
    email: str,
    password: str,
) -> tuple[User, str, str]:
    normalized = email.strip().lower()
    if await users_repo.get_by_email(session, normalized) is not None:
        raise EmailAlreadyRegisteredError(normalized)

    user = User(email=normalized, hashed_password=hash_password(password))
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError as exc:
        raise EmailAlreadyRegisteredError(normalized) from exc
    await session.commit()
    logger.info("Registered new user id=%s", user.id)
    return user, *tokens.issue_token_pair(user.id)


async def login_user(
    session: AsyncSession,
    email: str,
    password: str,
) -> tuple[User, str, str]:
    user = await users_repo.get_by_email(session, email.strip().lower())
    if user is None:
        # Ответ по времени не отличается от проверки существующего пользователя.
        verify_password(password, None)
        raise InvalidCredentialsError()
    if not user.is_active or not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError()
    return user, *tokens.issue_token_pair(user.id)


async def refresh_token_pair(
    session: AsyncSession,
    refresh_token: str,
) -> tuple[User, str, str]:
    user_id = tokens.decode_refresh_token(refresh_token)
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise InvalidRefreshTokenError()
    return user, *tokens.issue_token_pair(user.id)
