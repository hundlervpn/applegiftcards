"""Фоновый обход подписок к автосписанию.

См. docs/sdd-scrum-37-billing-scheduler.md
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.session import get_session_maker
from app.models.subscription import Subscription
from app.repositories.subscriptions import SubscriptionRepository
from app.services.recurring_payments import RecurringChargeError, charge_subscription

logger = logging.getLogger(__name__)

Charge = Callable[[AsyncSession, Subscription], Awaitable[object]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _take_cancellation() -> tuple[asyncio.Task[object] | None, int]:
    """Снимает отмену текущей задачи и возвращает, сколько раз её запросили.

    Пока счётчик больше нуля, следующий ``await`` сразу бросает ``CancelledError``
    и сессию уже не закрыть. После уборки счётчик нужно вернуть: иначе остановка
    приложения потеряется и цикл перейдёт к следующей подписке.
    """
    current = asyncio.current_task()
    if current is None:
        return None, 0
    count = 0
    while current.cancelling():
        current.uncancel()
        count += 1
    return current, count


def _restore_cancellation(current: asyncio.Task[object] | None, count: int) -> None:
    if current is None:
        return
    for _ in range(count):
        current.cancel()


async def _protect(awaitable: Awaitable[object]) -> None:
    current, count = _take_cancellation()
    try:
        await awaitable
    finally:
        _restore_cancellation(current, count)


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.exception("Could not roll back a billing session")


async def _close_session(session: AsyncSession) -> None:
    if session.in_transaction():
        await _rollback(session)
    await session.close()


async def _bill_locked_subscription(
    session: AsyncSession,
    subscription: Subscription,
    charge: Charge,
) -> None:
    subscription_id = subscription.id
    try:
        await charge(session, subscription)
    except asyncio.CancelledError:
        raise
    except RecurringChargeError as exc:
        logger.warning(
            "Recurring billing for subscription %s failed (%s)",
            subscription_id,
            type(exc).__name__,
        )
        await _rollback(session)
    except Exception:
        logger.exception(
            "Recurring billing for subscription %s failed",
            subscription_id,
        )
        await _rollback(session)


async def _finish_charge(
    session: AsyncSession,
    subscription: Subscription,
    charge: Charge,
) -> None:
    task = asyncio.create_task(
        _bill_locked_subscription(session, subscription, charge),
        name=f"billing-charge-{subscription.id}",
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await _protect(task)
        raise


async def run_billing_tick(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime] | None = None,
    charge: Charge = charge_subscription,
) -> None:
    """Списывает каждую подошедшую подписку не больше одного раза."""
    clock = now or _utc_now
    seen: set[int] = set()
    while True:
        session = session_maker()
        try:
            subscription = await SubscriptionRepository(session).lock_next_due_for_billing(
                as_of=clock(),
                exclude_ids=seen,
            )
            if subscription is None or subscription.id is None:
                return
            seen.add(subscription.id)
            await _finish_charge(session, subscription, charge)
        finally:
            await _protect(_close_session(session))


async def run_billing_scheduler(
    *,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    interval_seconds: float | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    charge: Charge = charge_subscription,
) -> None:
    """Первый проход сразу, дальше пауза ``interval_seconds`` после каждого прохода."""
    maker = get_session_maker() if session_maker is None else session_maker
    interval = (
        float(get_settings().billing_scheduler_interval_seconds)
        if interval_seconds is None
        else float(interval_seconds)
    )
    pause = sleep or asyncio.sleep
    while True:
        try:
            await run_billing_tick(maker, now=now, charge=charge)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Billing tick failed")
        await pause(interval)
