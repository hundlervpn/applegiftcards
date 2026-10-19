import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.repositories import gift_cards as gift_cards_repo
from app.schemas.admin import GiftCardBatchCreate, NominalStats
from app.services.crypto import encrypt_code

logger = logging.getLogger(__name__)


class AdminInventoryUnavailable(Exception):
    pass


def _build_cards(batch: GiftCardBatchCreate) -> list[GiftCard]:
    cards: list[GiftCard] = []
    for item in batch.cards:
        for code in item.codes:
            cards.append(
                GiftCard(
                    nominal_usd=item.nominal_usd,
                    code_encrypted=encrypt_code(code),
                    status=GiftCardStatus.available,
                )
            )
    return cards


async def load_gift_cards_batch(session: AsyncSession, batch: GiftCardBatchCreate) -> int:
    cards = _build_cards(batch)
    try:
        added = await gift_cards_repo.add_batch(session, cards)
    except SQLAlchemyError as exc:
        logger.warning("Admin gift card batch could not be saved")
        raise AdminInventoryUnavailable("Could not save gift card batch") from exc
    logger.info("Admin batch upload added %d gift cards", added)
    return added


async def get_stock_stats(session: AsyncSession) -> list[NominalStats]:
    try:
        rows = await gift_cards_repo.count_by_status(session)
    except SQLAlchemyError as exc:
        logger.warning("Admin stock statistics could not be read")
        raise AdminInventoryUnavailable("Could not read stock statistics") from exc
    return [
        NominalStats(
            nominal_usd=nominal_usd,
            available=available,
            reserved=reserved,
            sold=sold,
        )
        for nominal_usd, available, reserved, sold in rows
    ]
