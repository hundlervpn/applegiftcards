from fastapi import BackgroundTasks

from app.services.alerts import notify_low_stock_task
from app.services.email import send_gift_card_code_email
from app.services.order_fulfillment import GiftCardDelivery


def schedule_gift_card_code_email(
    background_tasks: BackgroundTasks,
    delivery: GiftCardDelivery,
) -> None:
    background_tasks.add_task(
        send_gift_card_code_email,
        delivery,
    )


def schedule_low_stock_alert(background_tasks: BackgroundTasks) -> None:
    background_tasks.add_task(notify_low_stock_task)
