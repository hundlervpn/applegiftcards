from fastapi import BackgroundTasks

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
