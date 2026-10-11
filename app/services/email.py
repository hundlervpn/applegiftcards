import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailConfigurationError(Exception):
    pass


async def send_gift_card_code_email(
    recipient_email: str,
    gift_card_code: str,
) -> None:
    settings = get_settings()
    smtp_password = settings.smtp_password.get_secret_value()
    if not settings.smtp_user or not smtp_password:
        raise EmailConfigurationError("SMTP_USER and SMTP_PASSWORD must be configured")
    if not recipient_email or not gift_card_code:
        raise ValueError("Recipient email and gift card code are required")

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = recipient_email
    message["Subject"] = "Ваш код Apple Gift Card"
    message.set_content(
        "Спасибо за покупку Apple Gift Card.\n\n"
        f"Ваш код: {gift_card_code}\n"
    )

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
