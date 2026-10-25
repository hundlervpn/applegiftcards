from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import (
    SUBSCRIPTION_CANCELLATION_RESPONSE,
    SUBSCRIPTION_FORBIDDEN_RESPONSE,
    SUBSCRIPTION_NOT_FOUND_RESPONSE,
    UNAUTHORIZED_RESPONSE,
)
from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.subscription import SubscriptionCancellationResponse
from app.services.subscription_cancellation import (
    SubscriptionAccessDenied,
    SubscriptionNotFound,
    cancel_subscription as cancel_user_subscription,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

SubscriptionId = Annotated[
    int,
    Path(
        ge=1,
        le=2**63 - 1,
        description="Идентификатор подписки",
        examples=[42],
    ),
]


@router.post(
    "/{subscription_id}/cancel",
    response_model=SubscriptionCancellationResponse,
    summary="Отменить автопродление подписки",
    description=(
        "Переводит подписку владельца JWT access-токена в cancelled и фиксирует время "
        "отмены в updated_at. После этого планировщик не выбирает её ни как active, "
        "ни как past_due. Повторный вызов возвращает уже отменённую подписку и не "
        "отправляет письмо ещё раз. Чужая подписка — 403, несуществующий id — 404. "
        "Подтверждение уходит на email учётной записи владельца."
    ),
    responses={
        200: SUBSCRIPTION_CANCELLATION_RESPONSE,
        401: UNAUTHORIZED_RESPONSE,
        403: SUBSCRIPTION_FORBIDDEN_RESPONSE,
        404: SUBSCRIPTION_NOT_FOUND_RESPONSE,
    },
)
async def cancel_subscription(
    subscription_id: SubscriptionId,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> SubscriptionCancellationResponse:
    try:
        result = await cancel_user_subscription(
            session,
            subscription_id=subscription_id,
            user_id=user.id,
        )
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail="Subscription not found") from exc
    except SubscriptionAccessDenied as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc
    return SubscriptionCancellationResponse(
        id=result.id,
        status=result.status,
        updated_at=result.updated_at,
    )
