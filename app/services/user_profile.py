import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.subscriptions import SubscriptionRepository

logger = logging.getLogger(__name__)


async def deactivate_user(session: AsyncSession, user: User) -> None:
    user_id = user.id
    try:
        user.is_active = False
        await session.flush()
        await SubscriptionRepository(session).cancel_billable_for_user(user_id)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    logger.info("Deactivated user id=%s", user_id)
