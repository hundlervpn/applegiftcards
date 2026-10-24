import asyncio
import base64

import httpx
import pytest

from app.config import Settings
from app.services import payment_methods
from app.services.payment_methods import get_card_masks

SECRET = "private-test-api-key"
METHOD_ID = "22e12f66-000f-5000-8000-18db351245c7"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused/unused",
        yookassa_shop_id="test-shop",
        yookassa_secret_key=SECRET,
    )


def install_transport(monkeypatch, handler):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        payment_methods.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )


def method_payload(method_id=METHOD_ID):
    return {
        "id": method_id,
        "type": "bank_card",
        "card": {"last4": "0042", "number": "5555555555550042", "csc": "123"},
        "title": "private title",
        "metadata": {"secret": SECRET},
    }


async def test_queries_saved_payment_method_and_returns_only_mask(settings, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=method_payload())

    install_transport(monkeypatch, handler)
    result = await get_card_masks([METHOD_ID, METHOD_ID], settings)
    assert result == {METHOD_ID: "**** 0042"}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"https://api.yookassa.ru/v3/payment_methods/{METHOD_ID}"
    expected = base64.b64encode(f"test-shop:{SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.extensions["timeout"] == {key: 5.0 for key in ("connect", "read", "write", "pool")}


@pytest.mark.parametrize("field", ["yookassa_shop_id", "yookassa_secret_key"])
async def test_missing_credentials_do_not_call_provider(settings, monkeypatch, field):
    setattr(settings, field, "")
    factory = lambda **kwargs: pytest.fail("Unexpected provider call")
    monkeypatch.setattr(payment_methods.httpx, "AsyncClient", factory)
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}


async def test_empty_ids_do_not_create_http_client(settings, monkeypatch):
    monkeypatch.setattr(
        payment_methods.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Unexpected provider call"),
    )
    assert await get_card_masks([], settings) == {}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_provider_http_errors_hide_sensitive_response(settings, monkeypatch, caplog, status):
    install_transport(monkeypatch, lambda request: httpx.Response(status, text=SECRET))
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}
    assert f"HTTP {status}" in caplog.text
    assert SECRET not in caplog.text
    assert METHOD_ID not in caplog.text


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_errors_do_not_expose_exception_details(settings, monkeypatch, caplog, error_type):
    def handler(request):
        raise error_type(SECRET, request=request)

    install_transport(monkeypatch, handler)
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}
    assert "network error" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("payload", [None, [], {}, {"id": METHOD_ID}, {"id": "other", "type": "bank_card", "card": {"last4": "0042"}}])
async def test_invalid_response_is_not_exposed(settings, monkeypatch, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}


async def test_invalid_json_is_not_exposed(settings, monkeypatch, caplog):
    install_transport(monkeypatch, lambda request: httpx.Response(200, text=SECRET))
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}
    assert SECRET not in caplog.text


@pytest.mark.parametrize("last4", ["", "123", "12345", "5555555555550042", "abcd", "１２３４", 1234, None])
async def test_only_four_ascii_digits_can_be_returned(settings, monkeypatch, last4):
    payload = method_payload()
    payload["card"]["last4"] = last4
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}


@pytest.mark.parametrize(
    "payload",
    [
        {"id": METHOD_ID, "type": "sbp", "sbp_operation_id": "private"},
        {"id": METHOD_ID, "type": "sbp", "card": {"last4": "0042"}},
        {"id": METHOD_ID, "type": "bank_card"},
        {"id": METHOD_ID, "type": "bank_card", "card": None},
    ],
)
async def test_non_card_or_missing_card_returns_null(settings, monkeypatch, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert await get_card_masks([METHOD_ID], settings) == {METHOD_ID: None}


@pytest.mark.parametrize("method_id", ["", " ", ".", ".."])
async def test_invalid_identifier_is_not_sent_to_provider(settings, monkeypatch, method_id):
    install_transport(monkeypatch, lambda request: pytest.fail("Unexpected provider call"))
    assert await get_card_masks([method_id], settings) == {method_id: None}


async def test_unique_method_lookups_have_bounded_concurrency(settings, monkeypatch):
    running = 0
    peak = 0
    calls = []

    async def handler(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        method_id = request.url.path.rsplit("/", 1)[-1]
        calls.append(method_id)
        await asyncio.sleep(0)
        running -= 1
        return httpx.Response(200, json=method_payload(method_id))

    install_transport(monkeypatch, handler)
    ids = [f"pm_{index}" for index in range(12)]
    result = await get_card_masks(ids + ids, settings)
    assert result == {method_id: "**** 0042" for method_id in ids}
    assert sorted(calls) == sorted(ids)
    assert 1 < peak <= 5


async def test_one_failure_does_not_hide_other_masks(settings, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/pm_missing"):
            return httpx.Response(404, text=SECRET)
        return httpx.Response(200, json=method_payload(METHOD_ID))

    install_transport(monkeypatch, handler)
    assert await get_card_masks([METHOD_ID, "pm_missing"], settings) == {
        METHOD_ID: "**** 0042", "pm_missing": None,
    }
