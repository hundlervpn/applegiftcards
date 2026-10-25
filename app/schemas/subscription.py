from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SubscriptionStatus


class SubscriptionCancellationResponse(BaseModel):
    id: int = Field(description="Идентификатор подписки", examples=[42])
    status: SubscriptionStatus = Field(
        description="После отмены всегда cancelled, в том числе при повторном вызове",
        examples=["cancelled"],
    )
    updated_at: datetime = Field(
        description=(
            "Время отмены, записанное в updated_at. "
            "Повторная отмена возвращает прежнее значение."
        ),
    )
