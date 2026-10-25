"""Отмена автопродления подписки владельцем.

См. docs/sdd-scrum-39-cancel-subscription.md
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SubscriptionStatus
from app.repositories import users as users_repo
from app.repositories.subscriptions import SubscriptionRepository
from app.services.email import (
    SubscriptionCancelledNotice,
    send_subscription_cancelled_email,
)

logger = logging.getLogger(__name__)


class SubscriptionNotFound(Exception):
    """Подписки с таким id нет."""


class SubscriptionAccessDenied(Exception):
    """Подписка принадлежит другому пользователю."""


@dataclass(frozen=True)
class SubscriptionCancellation:
    id: int
    status: SubscriptionStatus
    updated_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("cancellation time must be timezone-aware")


async def _release_lock(session: AsyncSession) -> None:
    """Снимает FOR UPDATE, не помечая уже прочитанного пользователя просроченным."""
    session.expire_on_rollback = False
    await session.rollback()


async def cancel_subscription(
    session: AsyncSession,
    *,
    subscription_id: int,
    user_id: int,
    now: Callable[[], datetime] | None = None,
) -> SubscriptionCancellation:
    """Переводит подписку владельца в cancelled и один раз ставит письмо.

    Повторный вызов не меняет строку и письмо не шлёт. Чужой user_id — отказ
    до изменения. Строка блокируется, поэтому списание, уже держащее её,
    заканчивается раньше, а следующее видит cancelled.
    """
    clock = now or _utc_now
    locked = await SubscriptionRepository(session).get_for_update(subscription_id)
    if locked is None or locked.id is None:
        raise SubscriptionNotFound("Subscription not found")
    if locked.user_id != user_id:
        await _release_lock(session)
        raise SubscriptionAccessDenied("Forbidden")

    if locked.status is SubscriptionStatus.cancelled:
        result = SubscriptionCancellation(
            id=locked.id,
            status=locked.status,
            updated_at=locked.updated_at,
        )
        await _release_lock(session)
        return result

    cancelled_at = clock()
    _require_aware(cancelled_at)
    owner = await users_repo.get_by_id(session, locked.user_id)
    recipient = owner.email if owner is not None else ""
    locked.status = SubscriptionStatus.cancelled
    locked.updated_at = cancelled_at
    result = SubscriptionCancellation(
        id=locked.id,
        status=locked.status,
        updated_at=cancelled_at,
    )
    notice = SubscriptionCancelledNotice(
        subscription_id=locked.id,
        recipient_email=recipient,
        nominal_usd=locked.nominal_usd,
        cancelled_at=cancelled_at,
    )
    await session.commit()
    logger.info("Subscription %s cancelled", result.id)
    if recipient.strip():
        try:
            enqueue_subscription_cancelled_email(notice)
        except Exception:
            logger.exception(
                "Could not enqueue cancellation email for subscription %s",
                result.id,
            )
    else:
        logger.warning(
            "Subscription %s cancelled without a recipient email",
            result.id,
        )
    return result


def enqueue_subscription_cancelled_email(notice: SubscriptionCancelledNotice) -> None:
    """Ставит подтверждающее письмо. Сервис не импортирует FastAPI."""
    loop = asyncio.get_running_loop()
    loop.create_task(
        _send_cancellation_email(notice),
        name=f"subscription-cancelled-email-{notice.subscription_id}",
    )


async def _send_cancellation_email(notice: SubscriptionCancelledNotice) -> None:
    try:
        await send_subscription_cancelled_email(notice)
    except Exception:
        logger.exception(
            "Cancellation email for subscription %s was not sent",
            notice.subscription_id,
        )
