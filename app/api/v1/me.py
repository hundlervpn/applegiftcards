from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import (
    FORBIDDEN_RESPONSE,
    ORDER_HISTORY_RESPONSE,
    UNAUTHORIZED_RESPONSE,
    error_response,
)
from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.me import OrderHistoryResponse
from app.services.order_history import OrderHistoryUnavailable, list_user_orders

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/orders",
    response_model=OrderHistoryResponse,
    summary="История заказов текущего пользователя",
    description=(
        "Возвращает только заказы владельца JWT access-токена, от новых к старым. "
        "Поддерживает limit/offset; total содержит число всех его заказов до пагинации. "
        "Для paid и delivered возвращаются купленные карты с расшифрованными кодами; "
        "для остальных статусов cards пуст. Идентификатор пользователя из параметров запроса не используется."
    ),
    responses={
        200: ORDER_HISTORY_RESPONSE,
        401: UNAUTHORIZED_RESPONSE,
        403: FORBIDDEN_RESPONSE,
        503: error_response(
            "Не удалось прочитать историю заказов или расшифровать код карты.",
            unavailable="Order history is unavailable",
        ),
    },
)
async def get_my_orders(
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100, description="Размер страницы: от 1 до 100 заказов"),
    offset: int = Query(default=0, ge=0, description="Количество заказов, пропускаемых с начала истории"),
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
