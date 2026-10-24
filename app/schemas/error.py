from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str = Field(description="Причина отказа в выполнении запроса")
