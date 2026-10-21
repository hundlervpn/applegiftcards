import logging
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import get_settings
from app.services.order_fulfillment import GiftCardDelivery

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


class EmailConfigurationError(Exception):
    pass


def build_gift_card_email(delivery: GiftCardDelivery, from_email: str) -> EmailMessage:
    """Собирает письмо с кодами: plain-text версия + HTML через Jinja2."""
    items = [dict(item) for item in delivery.items]
    nominal_usd = items[0]["nominal_usd"] if items else None
    order_date = (
        delivery.created_at.strftime("%d.%m.%Y %H:%M UTC")
        if delivery.created_at is not None
        else ""
    )
    total_amount_rub = (
        f"{delivery.total_amount_rub:.2f}"
        if delivery.total_amount_rub is not None
        else ""
    )

    environment = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    html = environment.get_template("email/gift_card_delivered.html").render(
        order_id=delivery.order_id,
        order_date=order_date,
        total_amount_rub=total_amount_rub,
        nominal_usd=nominal_usd,
        codes=list(delivery.gift_card_codes),
        items=items,
        recipient_email=delivery.recipient_email,
    )

    codes_text = "\n".join(delivery.gift_card_codes)
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = delivery.recipient_email
    message["Subject"] = f"Ваш код Apple Gift Card — заказ №{delivery.order_id}"
    message.set_content(
        "Спасибо за покупку Apple Gift Card в «Окне в Европу».\n\n"
        f"Заказ №{delivery.order_id} от {order_date} на сумму {total_amount_rub} ₽\n\n"
        "Ваш код активации:\n"
        f"{codes_text}\n\n"
        "Как активировать: откройте App Store → аватар профиля → "
        "«Redeem Gift Card or Code» и введите код."
    )
    message.add_alternative(html, subtype="html")
    return message


async def send_gift_card_code_email(delivery: GiftCardDelivery) -> None:
    settings = get_settings()
    smtp_password = settings.smtp_password.get_secret_value()
    if not settings.smtp_user or not smtp_password:
        raise EmailConfigurationError("SMTP_USER and SMTP_PASSWORD must be configured")
    if not delivery.recipient_email or not delivery.gift_card_codes:
        raise ValueError("Recipient email and gift card codes are required")

    message = build_gift_card_email(delivery, from_email=settings.smtp_from_email)

    tls_options = (
        {"use_tls": True}
        if settings.smtp_use_tls
        else {"start_tls": settings.smtp_starttls}
    )
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=smtp_password,
            timeout=settings.smtp_timeout_seconds,
            **tls_options,
        )
    except aiosmtplib.SMTPException:
        logger.exception("Failed to send Apple Gift Card email")
        raise
