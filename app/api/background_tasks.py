from fastapi import BackgroundTasks

from app.services.alerts import notify_low_stock_task
from app.services.email import send_gift_card_code_email


def schedule_gift_card_code_email(
    background_tasks: BackgroundTasks,
    recipient_email: str,
    gift_card_code: str,
) -> None:
    background_tasks.add_task(
        send_gift_card_code_email,
        recipient_email,
        gift_card_code,
    )


def schedule_low_stock_alert(background_tasks: BackgroundTasks) -> None:
    background_tasks.add_task(notify_low_stock_task)
