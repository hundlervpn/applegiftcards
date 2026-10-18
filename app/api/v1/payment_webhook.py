from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.background_tasks import schedule_gift_card_code_email, schedule_low_stock_alert
from app.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.payment_webhook import YooKassaNotification
from app.services.order_fulfillment import (
    OrderFulfillmentUnavailable,
    fulfill_verified_payment,
    mark_order_delivered,
)
from app.services.payment_webhooks import (
    InvalidYooKassaNotification,
    YooKassaVerificationUnavailable,
    verify_yookassa_notification,
)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook", status_code=200)
async def receive_payment_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db),
) -> Response:
    try:
        notification = YooKassaNotification.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid payment notification payload",
        ) from exc

    try:
        verified_payment = await verify_yookassa_notification(notification, settings)
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

    try:
        delivery = await fulfill_verified_payment(session, verified_payment)
    except OrderFulfillmentUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not complete the paid order",
        ) from exc

    if delivery is not None:
        schedule_gift_card_code_email(
            background_tasks,
            delivery.recipient_email,
            "\n".join(delivery.gift_card_codes),
        )
        background_tasks.add_task(mark_order_delivered, delivery.order_id)
        # Карты переведены в sold — проверяем остатки и оповещаем админа.
        schedule_low_stock_alert(background_tasks)

    return Response(status_code=200)
