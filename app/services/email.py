import logging
from datetime import timezone
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import get_settings
from app.services.dunning import (
    MAX_DUNNING_ATTEMPTS,
    DunningNotice,
    retry_interval_phrase,
)
from app.services.order_fulfillment import GiftCardDelivery

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


class EmailConfigurationError(Exception):
    pass


def _templates() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )


def _smtp_password(settings) -> str:
    password = settings.smtp_password.get_secret_value()
    if not settings.smtp_user or not password:
        raise EmailConfigurationError("SMTP_USER and SMTP_PASSWORD must be configured")
    return password


async def _deliver(message: EmailMessage, *, failure_log: str) -> None:
    settings = get_settings()
    password = _smtp_password(settings)
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
            password=password,
            timeout=settings.smtp_timeout_seconds,
            **tls_options,
        )
    except aiosmtplib.SMTPException:
        logger.exception(failure_log)
        raise


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

    html = _templates().get_template("email/gift_card_delivered.html").render(
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


def build_dunning_email(notice: DunningNotice, from_email: str) -> EmailMessage:
    """Сервисное письмо: предупреждение о ретрае или аннулирование автодоставки."""
    if notice.kind not in {"retry", "cancelled"}:
        raise ValueError("unknown dunning notice")
    if not notice.recipient_email or not notice.reason or not from_email:
        raise ValueError("recipient, reason and sender are required")
    if notice.kind == "retry" and (
        notice.next_billing_at is None
        or notice.retry_days is None
        or not notice.card_update_url
    ):
        raise ValueError("retry notice requires the next attempt and a card update url")

    if notice.kind == "cancelled":
        subject = f"Автодоставка по подписке №{notice.subscription_id} аннулирована"
        heading = "Автодоставка аннулирована"
        intro = (
            "Три списания подряд не прошли. Подписка закрыта, "
            "дальнейшие автосписания выполняться не будут."
        )
        preheader = "Автодоставка по подписке аннулирована"
        next_attempt = ""
        retry_phrase = ""
        plain = (
            "Автодоставка аннулирована.\n\n"
            f"Подписка №{notice.subscription_id}, номинал ${notice.nominal_usd}.\n"
            f"Причина последнего отказа: {notice.reason}\n"
            f"Неудачных попыток: {notice.attempts} из {MAX_DUNNING_ATTEMPTS}.\n\n"
            "Дальнейшие списания по этой подписке выполняться не будут."
        )
    else:
        billing_at = notice.next_billing_at
        retry_days = notice.retry_days
        if billing_at is None or retry_days is None:
            raise ValueError("retry notice requires the next attempt and a card update url")
        subject = f"Не удалось списать оплату подписки №{notice.subscription_id}"
        heading = "Не удалось списать оплату"
        intro = (
            "Банк не подтвердил автосписание. Мы повторим попытку позже. "
            "Чтобы не ждать, обновите карту по ссылке ниже."
        )
        preheader = "Не удалось списать оплату автодоставки"
        next_attempt = billing_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        retry_phrase = retry_interval_phrase(retry_days)
        plain = (
            "Не удалось списать оплату автодоставки.\n\n"
            f"Подписка №{notice.subscription_id}, номинал ${notice.nominal_usd}.\n"
            f"Причина: {notice.reason}\n"
            f"Следующая попытка: {next_attempt} (через {retry_phrase}).\n"
            f"Попытка {notice.attempts} из {MAX_DUNNING_ATTEMPTS}.\n\n"
            f"Обновить карту: {notice.card_update_url}"
        )

    html = _templates().get_template("email/subscription_dunning.html").render(
        subject=subject,
        preheader=preheader,
        heading=heading,
        intro=intro,
        kind=notice.kind,
        subscription_id=notice.subscription_id,
        nominal_usd=notice.nominal_usd,
        reason=notice.reason,
        attempts=notice.attempts,
        max_attempts=MAX_DUNNING_ATTEMPTS,
        next_attempt=next_attempt,
        retry_phrase=retry_phrase,
        card_update_url=notice.card_update_url,
        recipient_email=notice.recipient_email,
    )
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = notice.recipient_email
    message["Subject"] = subject
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return message


async def send_gift_card_code_email(delivery: GiftCardDelivery) -> None:
    settings = get_settings()
    _smtp_password(settings)
    if not delivery.recipient_email or not delivery.gift_card_codes:
        raise ValueError("Recipient email and gift card codes are required")

    message = build_gift_card_email(delivery, from_email=settings.smtp_from_email)
    await _deliver(message, failure_log="Failed to send Apple Gift Card email")


async def send_dunning_notice(notice: DunningNotice) -> None:
    settings = get_settings()
    _smtp_password(settings)
    if not notice.recipient_email or not notice.reason:
        raise ValueError("recipient email and decline reason are required")

    message = build_dunning_email(notice, from_email=settings.smtp_from_email)
    await _deliver(message, failure_log="Failed to send subscription dunning email")
