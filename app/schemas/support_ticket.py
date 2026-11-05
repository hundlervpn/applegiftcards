from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SupportTicketStatus


class SupportTicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order_id: int | None = Field(
        default=None,
        gt=0,
        le=9223372036854775807,
        strict=True,
        description="Идентификатор собственного заказа или null для общего вопроса",
        examples=[123],
    )
    subject: str = Field(
        min_length=5, max_length=120, description="Тема обращения", examples=["Код не активируется"]
    )
    message: str = Field(
        min_length=10,
        max_length=2000,
        description="Описание проблемы; пробелы по краям удаляются",
        examples=["При активации подарочной карты появляется ошибка. Прошу проверить заказ."],
    )


class SupportTicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Идентификатор обращения", examples=[1])
    order_id: int | None = Field(description="Связанный заказ или null")
    subject: str = Field(description="Тема обращения")
    message: str = Field(description="Описание проблемы")
    status: SupportTicketStatus = Field(description="Текущий статус обращения")
    created_at: datetime = Field(description="Дата создания с часовым поясом")


class SupportTicketListResponse(BaseModel):
    tickets: list[SupportTicketResponse] = Field(description="Страница собственных обращений")
    total: int = Field(ge=0, description="Количество всех собственных обращений до пагинации")
