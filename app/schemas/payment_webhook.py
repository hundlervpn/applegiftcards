from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.money import Rub


class PaymentAmount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Rub
    currency: str = Field(min_length=3, max_length=3)


class PaymentMethod(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    saved: bool | None = None


class NotificationPayment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    amount: PaymentAmount
    metadata: dict[str, Any] = Field(default_factory=dict)
    payment_method: PaymentMethod | None = None


class YooKassaNotification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["notification"]
    event: str = Field(min_length=1, max_length=64)
    object: NotificationPayment


class VerifiedPayment(NotificationPayment):
    pass
