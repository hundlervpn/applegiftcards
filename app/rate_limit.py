"""Лимиты по IP клиента для регистрации, входа и создания заказа.

Счётчики живут в памяти процесса (slowapi, скользящее окно): один воркер
uvicorn — один набор счётчиков. Проверка стоит в middleware и срабатывает
до зависимостей роута, поэтому отклонённый запрос не открывает сессию БД.
"""

import logging
import math
import time
from collections.abc import Callable, Iterable
from functools import lru_cache
from ipaddress import ip_address, ip_network

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

logger = logging.getLogger(__name__)

TOO_MANY_REQUESTS_DETAIL = "Too Many Requests"


def register_limit() -> str:
    return get_settings().auth_register_rate_limit


def login_limit() -> str:
    return get_settings().auth_login_rate_limit


def order_create_limit() -> str:
    return get_settings().order_create_rate_limit


def client_ip(request: Request) -> str:
    """Ключ лимита: адрес клиента, а не адрес Nginx.

    Заголовок X-Forwarded-For учитывается, только если прямой сосед — доверенный
    прокси. Nginx дописывает в конец адрес, с которого принял соединение,
    поэтому берётся самый правый корректный IP. Клиент, достучавшийся до
    приложения напрямую, не может выбрать себе корзину поддельным заголовком.
    """
    peer = request.client.host if request.client and request.client.host else None
    if peer and _is_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            chosen = _rightmost_ip(forwarded)
            if chosen is not None:
                return chosen
    return peer or "127.0.0.1"


def _is_trusted_proxy(host: str) -> bool:
    raw = get_settings().trusted_proxies
    if raw.strip() == "*":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in _trusted_networks(raw))


@lru_cache
def _trusted_networks(raw: str) -> tuple:
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" not in part:
            part = f"{part}/128" if ":" in part else f"{part}/32"
        networks.append(ip_network(part, strict=False))
    return tuple(networks)


def _rightmost_ip(header: str) -> str | None:
    for candidate in reversed(header.split(",")):
        parsed = _parse_ip(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_ip(candidate: str) -> str | None:
    text = candidate.strip().strip('"')
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    elif text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]
    try:
        return str(ip_address(text))
    except ValueError:
        return None


# Параметр key_func обязан называться request: slowapi передаёт его по имени.
limiter = Limiter(
    key_func=client_ip,
    headers_enabled=False,
    strategy="moving-window",
    storage_uri="memory://",
    key_style="url",
)


def rate_limit_headers(request: Request) -> dict[str, str]:
    current = getattr(request.state, "view_rate_limit", None)
    if not current:
        return {}
    item, identifiers = current
    stats = limiter.limiter.get_window_stats(item, *identifiers)
    retry_after = max(math.ceil(stats.reset_time - time.time()), 1)
    return {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit": str(item.amount),
        "X-RateLimit-Remaining": str(max(int(stats.remaining), 0)),
        "X-RateLimit-Reset": str(math.ceil(stats.reset_time)),
    }


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning(
        "rate limit exceeded on %s %s from %s",
        request.method,
        request.url.path,
        client_ip(request),
    )
    return JSONResponse(
        {"detail": TOO_MANY_REQUESTS_DETAIL},
        status_code=429,
        headers=rate_limit_headers(request),
    )


def _find_endpoint(routes: Iterable[object], scope: Scope) -> Callable | None:
    """Ищет эндпоинт и в плоском списке роутов, и во вложенных include_router FastAPI."""
    found: Callable | None = None
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            for ctx in contexts():
                match, _ = ctx.matches(scope)
                endpoint = getattr(ctx, "endpoint", None)
                if match == Match.FULL and endpoint is not None:
                    found = endpoint
            continue
        nested = getattr(route, "routes", None)
        if nested:
            nested_found = _find_endpoint(nested, scope)
            if nested_found is not None:
                found = nested_found
            continue
        endpoint = getattr(route, "endpoint", None)
        matches = getattr(route, "matches", None)
        if endpoint is not None and callable(matches):
            match, _ = matches(scope)
            if match == Match.FULL:
                found = endpoint
    return found


class RateLimitMiddleware:
    """Отсекает запрос до роута, если IP исчерпал лимит этого эндпоинта."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not limiter.enabled:
            await self.app(scope, receive, send)
            return

        handler = _find_endpoint(scope["app"].routes, scope)
        if handler is None:
            await self.app(scope, receive, send)
            return
        name = f"{handler.__module__}.{handler.__name__}"
        if name not in limiter._route_limits and name not in limiter._dynamic_route_limits:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            limiter._check_request_limit(request, handler, in_middleware=False)
        except RateLimitExceeded as exc:
            response = rate_limit_exceeded_handler(request, exc)
            await response(scope, receive, send)
            return
        # Декоратор slowapi на роуте не должен списать лимит второй раз.
        request.state._rate_limiting_complete = True
        await self.app(scope, receive, send)
