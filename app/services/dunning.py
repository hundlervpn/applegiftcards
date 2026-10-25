"""Повтор неудачного автосписания и закрытие подписки после трёх отказов.

См. docs/sdd-scrum-38-dunning-management.md
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.models.enums import SubscriptionStatus

# Лестница из задачи: 1, 3 и 5 суток. Третья неудача подписку закрывает,
# поэтому пятые сутки в календарь не ставятся: иначе планировщик списал бы
# её ещё раз. Индексы 0 и 1 — паузы после первой и второй неудачи.
DUNNING_RETRY_DAYS = (1, 3, 5)
MAX_DUNNING_ATTEMPTS = 3

TRANSPORT_FAILURE_REASON = (
    "Временная ошибка банка: платёжный сервис не подтвердил списание."
)
GENERIC_DECLINE_REASON = "Банк отклонил платёж."

_DECLINE_REASONS = {
    "insufficient_funds": "Недостаточно средств на карте.",
    "payment_method_restricted": "Карта заблокирована или банк ограничил платежи по ней.",
    "card_expired": "Срок действия карты истёк.",
    "issuer_unavailable": "Банк временно недоступен.",
    "internal_timeout": "Банк не ответил вовремя.",
    "general_decline": "Банк отклонил платёж.",
    "fraud_suspected": "Банк отклонил платёж из соображений безопасности.",
    "permission_revoked": "Сохранённый способ оплаты отозван.",
    "payment_method_limit_exceeded": "Превышен лимит платежей по карте.",
    "call_issuer": "Банк отклонил платёж и просит связаться с ним.",
}

_DEFAULT_CARD_UPDATE_URL = "http://localhost:8000/account/payment-method"


@dataclass(frozen=True)
class DunningPlan:
    status: SubscriptionStatus
    dunning_attempts: int
    next_billing_at: datetime
    retry_days: int | None

    @property
    def cancelled(self) -> bool:
        return self.status is SubscriptionStatus.cancelled


@dataclass(frozen=True)
class DunningNotice:
    kind: Literal["retry", "cancelled"]
    recipient_email: str
    subscription_id: int
    nominal_usd: int
    reason: str
    card_update_url: str
    attempts: int
    retry_days: int | None
    next_billing_at: datetime | None


def customer_facing_decline_reason(code: str | None) -> str:
    """Текст причины для письма. Неизвестный код кассы наружу не отдаём."""
    if code is None:
        return GENERIC_DECLINE_REASON
    return _DECLINE_REASONS.get(code, GENERIC_DECLINE_REASON)


def normalize_card_update_url(url: str | None) -> str:
    """Оставляет только http(s)-ссылку без перевода строки, иначе дефолт."""
    if url is None:
        return _DEFAULT_CARD_UPDATE_URL
    cleaned = url.strip()
    if not cleaned.startswith(("https://", "http://")):
        return _DEFAULT_CARD_UPDATE_URL
    if any(character in cleaned for character in ("\n", "\r", " ", "\t")):
        return _DEFAULT_CARD_UPDATE_URL
    return cleaned


def retry_interval_phrase(days: int) -> str:
    if days <= 0:
        raise ValueError("retry interval must be positive")
    mod10 = days % 10
    mod100 = days % 100
    if mod10 == 1 and mod100 != 11:
        unit = "день"
    elif mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}:
        unit = "дня"
    else:
        unit = "дней"
    return f"{days} {unit}"


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{label} must be timezone-aware")


def plan_dunning(
    attempts_before: int,
    *,
    now: datetime,
    billing_at: datetime,
) -> DunningPlan:
    """Следующее состояние подписки после отказа кассы.

    Первая неудача — пауза 1 сутки, вторая — 3 суток, обе в ``past_due``.
    Третья закрывает подписку: ``next_billing_at`` не сдвигается, выборка
    биллинга такую строку больше не берёт. Пауза считается от ``now``,
    а не от просроченной даты, чтобы опоздавший проход не поставил ретрай в прошлое.
    """
    if attempts_before < 0:
        raise ValueError("dunning attempts must be non-negative")
    _aware(now, "now")
    _aware(billing_at, "billing date")

    attempts = attempts_before + 1
    if attempts >= MAX_DUNNING_ATTEMPTS:
        return DunningPlan(
            status=SubscriptionStatus.cancelled,
            dunning_attempts=attempts,
            next_billing_at=billing_at,
            retry_days=None,
        )
    retry_days = DUNNING_RETRY_DAYS[attempts - 1]
    return DunningPlan(
        status=SubscriptionStatus.past_due,
        dunning_attempts=attempts,
        next_billing_at=now + timedelta(days=retry_days),
        retry_days=retry_days,
    )
