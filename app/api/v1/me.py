from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.me import OrderHistoryResponse
from app.services.order_history import OrderHistoryUnavailable, list_user_orders

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/orders", response_model=OrderHistoryResponse)
async def get_my_orders(
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> OrderHistoryResponse:
    try:
        orders, total = await list_user_orders(
            session,
            user.id,
            limit=limit,
            offset=offset,
        )
    except OrderHistoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Order history is unavailable") from exc
    return OrderHistoryResponse(orders=orders, total=total)
