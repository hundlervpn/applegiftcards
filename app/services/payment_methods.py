import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.services.payments import YOOKASSA_API_URL

logger = logging.getLogger(__name__)
_MAX_CONCURRENT_LOOKUPS = 5


class _Card(BaseModel):
    model_config = ConfigDict(extra="ignore")

    last4: str = Field(pattern=r"^[0-9]{4}$")


class _PaymentMethod(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    type: str
    card: _Card | None = None


async def _get_card_mask(client: httpx.AsyncClient, payment_method_id: str) -> str | None:
    if not payment_method_id.strip() or payment_method_id in {".", ".."}:
        logger.warning("YooKassa payment method lookup skipped: invalid identifier")
        return None

    try:
        response = await client.get(
            f"{YOOKASSA_API_URL}/payment_methods/{quote(payment_method_id, safe='')}"
        )
    except httpx.HTTPError:
        logger.warning("YooKassa payment method lookup failed due to a network error")
        return None

    if not response.is_success:
        logger.warning("YooKassa payment method lookup failed with HTTP %s", response.status_code)
        return None

    try:
        method = _PaymentMethod.model_validate(response.json())
    except (ValueError, ValidationError):
        logger.warning("YooKassa payment method lookup returned an invalid response")
        return None

    if method.id != payment_method_id:
        logger.warning("YooKassa payment method lookup returned a different identifier")
        return None
    if method.type != "bank_card" or method.card is None:
        return None
    return f"**** {method.card.last4}"


async def get_card_masks(
    payment_method_ids: Sequence[str],
    settings: Settings,
) -> dict[str, str | None]:
    unique_ids = list(dict.fromkeys(payment_method_ids))
    if not unique_ids:
        return {}
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        logger.warning("YooKassa payment method lookup credentials are not configured")
        return dict.fromkeys(unique_ids)

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)
    async with httpx.AsyncClient(
        auth=httpx.BasicAuth(settings.yookassa_shop_id, settings.yookassa_secret_key),
        timeout=5.0,
    ) as client:
        async def lookup(payment_method_id: str) -> str | None:
            async with semaphore:
                return await _get_card_mask(client, payment_method_id)

        masks = await asyncio.gather(*(lookup(method_id) for method_id in unique_ids))
    return dict(zip(unique_ids, masks, strict=True))
