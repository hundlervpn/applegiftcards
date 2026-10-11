"""Денежные суммы: целое число копеек (RUB) во всём проекте."""

from typing import Annotated

from pydantic import Field

# API, сервисы, БД — только int в копейках; не float и не Decimal.
Kopecks = Annotated[int, Field(ge=1, description="Сумма в копейках (RUB)")]


def kopecks_to_yookassa_rub(kopecks: int) -> str:
    """Строка рублей с копейками для YooKassa amount.value."""
    if kopecks <= 0:
        raise ValueError("kopecks must be positive")
    return f"{kopecks // 100}.{kopecks % 100:02d}"
