from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas.payment_webhook import YooKassaNotification
from app.services.payment_webhooks import (
    InvalidYooKassaNotification,
    YooKassaVerificationUnavailable,
    verify_yookassa_notification,
)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook", status_code=200)
async def receive_payment_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        notification = YooKassaNotification.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid payment notification payload",
        ) from exc

    try:
        await verify_yookassa_notification(notification, settings)
    except InvalidYooKassaNotification as exc:
        raise HTTPException(
            status_code=403,
            detail="Invalid payment notification",
        ) from exc
    except YooKassaVerificationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify notification with YooKassa",
        ) from exc

    return Response(status_code=200)
