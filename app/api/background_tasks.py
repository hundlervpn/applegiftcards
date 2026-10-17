from fastapi import BackgroundTasks

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
