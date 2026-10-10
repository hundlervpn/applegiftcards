from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel, ValidationError

YOOKASSA_API_URL = "https://api.yookassa.ru/v3"


class PaymentSession(BaseModel):
    payment_url: str
    external_payment_id: str


class AcquirerError(Exception):
    pass


class AcquirerUnavailableError(AcquirerError):
    pass


class AcquirerBadResponseError(AcquirerError):
    pass


async def create_payment(
    shop_id: str,
    secret_key: str,
    order_id: int,
    amount_minor: int,
    return_url: str,
    currency: str = "RUB",
    api_url: str = YOOKASSA_API_URL,
    timeout_seconds: float = 10.0,
) -> PaymentSession:
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")

    idempotence_key = str(uuid5(NAMESPACE_URL, f"giftcodes:yookassa:order:{order_id}"))
    payload = {
        "amount": {
            "value": f"{amount_minor // 100}.{amount_minor % 100:02d}",
            "currency": currency,
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": f"Заказ №{order_id}",
        "metadata": {"order_id": str(order_id)},
    }
    try:
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(shop_id, secret_key),
            timeout=timeout_seconds,
        ) as client:
            response = await client.post(
                f"{api_url.rstrip('/')}/payments",
                headers={"Idempotence-Key": idempotence_key},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise AcquirerUnavailableError(f"YooKassa unreachable: {exc}") from exc

    if not response.is_success:
        raise AcquirerBadResponseError(
            f"YooKassa returned {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AcquirerBadResponseError("YooKassa returned invalid JSON") from exc

    try:
        if not isinstance(data, dict):
            raise TypeError("response is not a JSON object")
        confirmation = data["confirmation"]
        return PaymentSession(
            payment_url=confirmation["confirmation_url"],
            external_payment_id=data["id"],
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise AcquirerBadResponseError(f"unexpected YooKassa response: {exc}") from exc
