import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.admin import AdminStatsResponse, GiftCardBatchCreate, GiftCardBatchResponse
from app.services.admin_inventory import (
    AdminInventoryUnavailable,
    get_stock_stats,
    load_gift_cards_batch,
)
from app.services.crypto import GiftCardCryptoError


async def require_admin_token(
    x_admin_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    configured_token = settings.admin_api_token.get_secret_value()
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="Admin API is not configured",
        )
    if x_admin_token is None:
        raise HTTPException(
            status_code=401,
            detail="X-Admin-Token header is required",
        )
    if not secrets.compare_digest(
        x_admin_token.encode("utf-8"),
        configured_token.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid admin token",
        )


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


@router.post("/gift-cards/batch", status_code=201, response_model=GiftCardBatchResponse)
async def upload_gift_cards_batch(
    body: GiftCardBatchCreate,
    session: AsyncSession = Depends(get_db),
) -> GiftCardBatchResponse:
    try:
        added = await load_gift_cards_batch(session, body)
    except GiftCardCryptoError as exc:
        raise HTTPException(
            status_code=503,
            detail="Gift card encryption is unavailable",
        ) from exc
    except AdminInventoryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Gift card storage is unavailable",
        ) from exc
    return GiftCardBatchResponse(added=added)


@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    session: AsyncSession = Depends(get_db),
) -> AdminStatsResponse:
    try:
        nominals = await get_stock_stats(session)
    except AdminInventoryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Gift card storage is unavailable",
        ) from exc
    return AdminStatsResponse(nominals=nominals)
