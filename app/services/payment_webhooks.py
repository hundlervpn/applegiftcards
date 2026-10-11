import logging
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas.payment_webhook import VerifiedPayment, YooKassaNotification

YOOKASSA_API_URL = "https://api.yookassa.ru/v3"
logger = logging.getLogger(__name__)
SUPPORTED_PAYMENT_STATUSES = {
    "pending",
    "waiting_for_capture",
    "succeeded",
    "canceled",
}


class InvalidYooKassaNotification(Exception):
    pass


class YooKassaVerificationUnavailable(Exception):
    pass


async def verify_yookassa_notification(
    notification: YooKassaNotification,
    settings: Settings,
) -> VerifiedPayment:
    payment = notification.object
    if payment.status not in SUPPORTED_PAYMENT_STATUSES:
        logger.warning("YooKassa webhook rejected: unsupported payment status")
        raise InvalidYooKassaNotification("Unsupported payment status")
    if notification.event != f"payment.{payment.status}":
        logger.warning("YooKassa webhook rejected: event and payment status do not match")
        raise InvalidYooKassaNotification("Event does not match payment status")
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        logger.error("YooKassa verification credentials are not configured")
        raise YooKassaVerificationUnavailable(
            "YooKassa verification credentials are not configured"
        )

    payment_id = quote(payment.id, safe="")
    try:
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(
                settings.yookassa_shop_id,
                settings.yookassa_secret_key,
            ),
            timeout=5.0,
        ) as client:
            response = await client.get(f"{YOOKASSA_API_URL}/payments/{payment_id}")
    except httpx.HTTPError as exc:
        logger.exception("YooKassa payment lookup failed due to a network error")
        raise YooKassaVerificationUnavailable(
            "Could not reach YooKassa to verify the payment"
        ) from exc

    if response.status_code in {401, 403}:
        logger.error(
            "YooKassa rejected API credentials with HTTP %s",
            response.status_code,
        )
        raise YooKassaVerificationUnavailable(
            f"YooKassa rejected API credentials with HTTP {response.status_code}"
        )
    if response.status_code == 429:
        logger.warning("YooKassa payment lookup was rate limited with HTTP 429")
        raise YooKassaVerificationUnavailable(
            "YooKassa rate limited the payment verification request"
        )
    if response.is_server_error:
        logger.warning(
            "YooKassa payment lookup failed with HTTP %s",
            response.status_code,
        )
        raise YooKassaVerificationUnavailable(
            f"YooKassa returned HTTP {response.status_code} during payment verification"
        )
    if response.status_code == 404:
        logger.warning("YooKassa payment lookup rejected: payment not found")
        raise InvalidYooKassaNotification("YooKassa payment was not found")
    if not response.is_success:
        logger.warning(
            "YooKassa payment lookup rejected with HTTP %s",
            response.status_code,
        )
        raise InvalidYooKassaNotification(
            f"YooKassa rejected the payment lookup with HTTP {response.status_code}"
        )

    try:
        response_data = response.json()
    except ValueError as exc:
        logger.warning("YooKassa payment lookup returned invalid JSON")
        raise YooKassaVerificationUnavailable(
            "YooKassa returned invalid JSON during payment verification"
        ) from exc
    try:
        verified_payment = VerifiedPayment.model_validate(response_data)
    except ValidationError as exc:
        logger.warning(
            "YooKassa payment response failed schema validation (%s errors)",
            exc.error_count(),
        )
        raise YooKassaVerificationUnavailable(
            "YooKassa returned an unexpected payment response"
        ) from exc

    mismatches = []
    if verified_payment.id != payment.id:
        mismatches.append("id")
    if verified_payment.status != payment.status:
        mismatches.append("status")
    if verified_payment.amount != payment.amount:
        mismatches.append("amount")
    if verified_payment.metadata != payment.metadata:
        mismatches.append("metadata")
    if mismatches:
        logger.warning(
            "YooKassa webhook rejected: payment data mismatch in %s",
            ", ".join(mismatches),
        )
        raise InvalidYooKassaNotification(
            "YooKassa payment data does not match the notification"
        )

    return verified_payment
