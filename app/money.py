"""Денежные суммы в рублях (RUB) во всём проекте."""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

# API, сервисы, БД — Decimal (Numeric(12, 2)); не float.
Rub = Annotated[Decimal, Field(gt=0, description="Сумма в рублях (RUB), до 2 знаков после запятой")]


def decimal_to_yookassa_rub(amount: Decimal) -> str:
    """Строка рублей с копейками для YooKassa amount.value."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    return f"{amount.quantize(Decimal('0.01')):.2f}"
