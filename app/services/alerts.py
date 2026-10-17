import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.repositories import catalog as catalog_repo

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
HTTP_TIMEOUT_SECONDS = 10.0


def build_low_stock_message(alerts: list[tuple[int, int]]) -> str:
    rows = "\n".join(
        f"• номинал ${nominal_usd} — свободных карт: {available_count}"
        for nominal_usd, available_count in alerts
    )
    return f"Заканчиваются подарочные карты:\n{rows}"


async def find_low_stock(session: AsyncSession) -> list[tuple[int, int]]:
    """Возвращает (номинал, остаток) для номиналов ниже порога."""
    threshold = get_settings().low_stock_threshold
    rows = await catalog_repo.list_catalog_rows(session)
    return [
        (nominal_usd, available_count)
        for nominal_usd, _price_rub, available_count in rows
        if available_count < threshold
    ]


async def send_telegram_message(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    text: str,
) -> None:
    response = await client.post(
        f"{TELEGRAM_API_URL}/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    )
    response.raise_for_status()


async def notify_low_stock(
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Проверяет остатки и шлёт алерт админу в Telegram.

    Ошибки не пробрасываются: сбой алерта не должен ломать заказ.
    """
    settings = get_settings()
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_admin_chat_id
    if not bot_token or not chat_id:
        logger.info("Telegram alerts are not configured, skipping low stock check")
        return

    try:
        alerts = await find_low_stock(session)
    except Exception:
        logger.warning("Could not check gift card stock for the low stock alert")
        return
    if not alerts:
        return

    text = build_low_stock_message(alerts)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as own_client:
                await send_telegram_message(own_client, bot_token, chat_id, text)
        else:
            await send_telegram_message(client, bot_token, chat_id, text)
    except httpx.HTTPError:
        logger.warning("Could not send the low stock Telegram alert")


async def notify_low_stock_task() -> None:
    """Фоновая задача: открывает собственную сессию к БД."""
    from app.db.session import get_session_maker

    async with get_session_maker()() as session:
        await notify_low_stock(session)
