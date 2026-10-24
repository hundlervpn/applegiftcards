from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.me_subscription import SubscriptionListResponse
from app.services.user_subscriptions import UserSubscriptionsUnavailable, list_user_subscriptions

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/subscriptions", response_model=SubscriptionListResponse)
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
