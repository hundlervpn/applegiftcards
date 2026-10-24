from typing import Any

from app.schemas.error import ErrorResponse

API_TAGS = [
    {
        "name": "auth",
        "description": (
            "Регистрация, вход и обновление JWT. Получите пару токенов через register или login, "
            "вставьте access_token в Authorize (BearerAuth) и обращайтесь к /me/*. "
            "refresh_token передаётся только в теле запроса /auth/refresh."
        ),
    },
    {
        "name": "me",
        "description": (
            "Личный кабинет авторизованного пользователя: заказы и подписки. "
            "Пользователь определяется по JWT access-токену; выбирать чужой user_id нельзя."
        ),
    },
]

TOKEN_PAIR_EXAMPLE = {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.demo-access.signature",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.demo-refresh.signature",
    "token_type": "bearer",
}

TOKEN_PAIR_RESPONSE = {
    "description": "Пара JWT access/refresh. Пример содержит демонстрационные токены.",
    "content": {"application/json": {"example": TOKEN_PAIR_EXAMPLE}},
}

ORDER_HISTORY_RESPONSE = {
    "description": "Страница заказов текущего пользователя и общее количество его заказов.",
    "content": {
        "application/json": {
            "examples": {
                "delivered": {
                    "summary": "Доставленный заказ с демонстрационным кодом",
                    "value": {
                        "orders": [
                            {
                                "id": 123,
                                "status": "delivered",
                                "total_amount_rub": "1500.00",
                                "created_at": "2026-10-24T12:00:00Z",
                                "paid_at": "2026-10-24T12:01:00Z",
                                "cards": [{"nominal_usd": 10, "code": "DEMO-CODE-DO-NOT-REDEEM"}],
                            },
                        ],
                        "total": 1,
                    },
                },
                "empty": {
                    "summary": "Заказов нет",
                    "value": {"orders": [], "total": 0},
                },
            },
        },
    },
}

SUBSCRIPTIONS_RESPONSE = {
    "description": "Подписки текущего пользователя, отсортированные от новых к старым.",
    "content": {
        "application/json": {
            "examples": {
                "active": {
                    "summary": "Активная подписка с маской карты",
                    "value": {
                        "subscriptions": [
                            {
                                "id": 42,
                                "nominal_usd": 10,
                                "status": "active",
                                "card_mask": "**** 4242",
                                "next_billing_at": "2026-11-23T12:00:00Z",
                                "price_rub": "1500.00",
                                "created_at": "2026-10-24T12:00:00Z",
                            },
                        ],
                    },
                },
                "mask_unavailable": {
                    "summary": "Подписка с задолженностью, маска карты недоступна",
                    "value": {
                        "subscriptions": [
                            {
                                "id": 42,
                                "nominal_usd": 10,
                                "status": "past_due",
                                "card_mask": None,
                                "next_billing_at": "2026-11-23T12:00:00Z",
                                "price_rub": "1500.00",
                                "created_at": "2026-10-24T12:00:00Z",
                            },
                        ],
                    },
                },
                "empty": {"summary": "Подписок нет", "value": {"subscriptions": []}},
            },
        },
    },
}


def error_response(description: str, *, bearer: bool = False, **examples: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "examples": {name: {"value": {"detail": detail}} for name, detail in examples.items()},
            },
        },
    }
    if bearer:
        response["headers"] = {
            "WWW-Authenticate": {"schema": {"type": "string"}, "example": "Bearer"},
        }
    return response


UNAUTHORIZED_RESPONSE = error_response(
    "Access-токен отсутствует, невалиден, истёк либо пользователь удалён или неактивен.",
    bearer=True,
    missing="Not authenticated",
    invalid="Invalid token",
    expired="Token has expired",
    inactive="User not found or inactive",
)

FORBIDDEN_RESPONSE = error_response(
    "Схема отказа в доступе (403). Сейчас /me/* не принимает идентификатор чужого пользователя: "
    "чужие записи исключаются из результата, а ошибки авторизации возвращаются как 401.",
    forbidden="Forbidden",
)

EMAIL_CONFLICT_RESPONSE = error_response(
    "Email уже зарегистрирован. Перед сравнением удаляются пробелы по краям и приводится регистр.",
    duplicate="User with this email already exists",
)

INVALID_CREDENTIALS_RESPONSE = error_response(
    "Неверный email или пароль либо пользователь неактивен. Ответ не раскрывает существование email.",
    bearer=True,
    invalid="Invalid email or password",
)

INVALID_REFRESH_RESPONSE = error_response(
    "Refresh-токен невалиден, имеет неверный тип, истёк либо пользователь удалён или неактивен.",
    invalid="Invalid refresh token",
    expired="Token has expired",
)
