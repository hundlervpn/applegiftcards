from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import (
    FORBIDDEN_RESPONSE,
    SUBSCRIPTIONS_RESPONSE,
    UNAUTHORIZED_RESPONSE,
    error_response,
)
from app.config import Settings, get_settings
from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.me_subscription import SubscriptionListResponse
from app.services.user_subscriptions import UserSubscriptionsUnavailable, list_user_subscriptions

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/subscriptions",
    response_model=SubscriptionListResponse,
    summary="Подписки текущего пользователя",
    description=(
        "Возвращает только подписки владельца JWT access-токена, от новых к старым, "
        "со статусами active, past_due и cancelled. Цена берётся из текущего каталога цен в рублях. "
        "Номер банковской карты доступен только как **** 4242. Если маска недоступна "
        "или способ оплаты не является банковской картой, card_mask равен null; "
        "сами подписки остаются доступны. Идентификатор платёжного метода не возвращается."
    ),
    responses={
        200: SUBSCRIPTIONS_RESPONSE,
        401: UNAUTHORIZED_RESPONSE,
        403: FORBIDDEN_RESPONSE,
        503: error_response(
            "Не удалось прочитать подписки из базы данных.",
            unavailable="Subscriptions are unavailable",
        ),
    },
)
async def get_my_subscriptions(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SubscriptionListResponse:
    try:
        subscriptions = await list_user_subscriptions(session, user.id, settings)
    except UserSubscriptionsUnavailable as exc:
        raise HTTPException(status_code=503, detail="Subscriptions are unavailable") from exc
    return SubscriptionListResponse(subscriptions=subscriptions)
