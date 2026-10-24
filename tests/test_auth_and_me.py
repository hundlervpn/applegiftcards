import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.auth import RefreshRequest, TokenResponse, UserLogin, UserRegister
from app.schemas.error import ErrorResponse
from app.schemas.me import OrderHistoryResponse
from app.schemas.me_subscription import SubscriptionListResponse
from app.services import tokens
from app.services.crypto import encrypt_code
from app.services.security import verify_password

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
ORDERS = "/api/v1/me/orders"
SUBSCRIPTIONS = "/api/v1/me/subscriptions"
ME_PATHS = (ORDERS, SUBSCRIPTIONS)
SECRET = "scrum-45-test-signing-secret-with-more-than-sixty-four-bytes-for-isolated-tests"
PASSWORD = "DemoPass123"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
    monkeypatch.setenv("REFRESH_TOKEN_EXPIRE_DAYS", "30")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def account(db_session):
    user = User(email="buyer@example.com")
    db_session.add(user)
    await db_session.commit()
    return user


async def register(client, email="buyer@example.com"):
    response = await client.post(REGISTER, json={"email": email, "password": PASSWORD})
    assert response.status_code == 201
    return response.json()


def authorization(token):
    return {"Authorization": f"Bearer {token}"}


def signed_token(user_id, *, token_type="access", secret=SECRET, algorithm="HS256", **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now - timedelta(minutes=2),
        "exp": now + timedelta(minutes=5),
    }
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm=algorithm)


async def seed_account(session, user):
    if await session.get(Price, 10) is None:
        session.add(Price(nominal_usd=10, price_rub=Decimal("1500.00")))
        await session.flush()
    order = Order(
        user_id=user.id,
        status=OrderStatus.delivered,
        items=[{"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"}],
        total_amount_rub=Decimal("1500.00"),
        created_at=NOW,
        paid_at=NOW + timedelta(minutes=1),
    )
    session.add(order)
    await session.flush()
    code = f"DEMO-CODE-{user.id}"
    session.add(
        GiftCard(
            nominal_usd=10,
            code_encrypted=encrypt_code(code),
            status=GiftCardStatus.sold,
            order_id=order.id,
            sale_price_rub=Decimal("1500.00"),
        ),
    )
    subscription = Subscription(
        user_id=user.id,
        nominal_usd=10,
        payment_method_id=f"pm_test_{user.id}",
        status=SubscriptionStatus.active,
        next_billing_at=NOW + timedelta(days=30),
    )
    session.add(subscription)
    await session.commit()
    return order, subscription, code


async def test_register_login_orders_subscriptions_and_refresh_end_to_end(client, db_session):
    registered = await register(client, "  Buyer@Example.COM  ")
    TokenResponse.model_validate(registered)
    user = await db_session.scalar(select(User).where(User.email == "buyer@example.com"))
    assert user is not None
    assert user.hashed_password != PASSWORD
    assert verify_password(PASSWORD, user.hashed_password)
    assert tokens.decode_access_token(registered["access_token"]) == user.id
    assert tokens.decode_refresh_token(registered["refresh_token"]) == user.id
    order, subscription, code = await seed_account(db_session, user)

    login = await client.post(LOGIN, json={"email": "  BUYER@example.com  ", "password": PASSWORD})
    assert login.status_code == 200
    pair = TokenResponse.model_validate(login.json())
    assert pair.token_type == "bearer"
    headers = authorization(pair.access_token)
    orders = await client.get(ORDERS, params={"limit": 1, "offset": 0}, headers=headers)
    assert orders.status_code == 200
    history = OrderHistoryResponse.model_validate(orders.json())
    assert history.total == 1
    assert history.orders[0].id == order.id
    assert history.orders[0].cards[0].code == code
    assert history.orders[0].cards[0].nominal_usd == 10
    assert history.orders[0].total_amount_rub == Decimal("1500.00")

    subscriptions = await client.get(SUBSCRIPTIONS, headers=headers)
    assert subscriptions.status_code == 200
    items = SubscriptionListResponse.model_validate(subscriptions.json()).subscriptions
    assert [item.id for item in items] == [subscription.id]
    assert items[0].status is SubscriptionStatus.active
    assert items[0].card_mask is None
    assert "payment_method_id" not in subscriptions.text

    refreshed = await client.post(REFRESH, json={"refresh_token": pair.refresh_token})
    assert refreshed.status_code == 200
    fresh_pair = TokenResponse.model_validate(refreshed.json())
    assert tokens.decode_access_token(fresh_pair.access_token) == user.id
    assert tokens.decode_refresh_token(fresh_pair.refresh_token) == user.id
    fresh_history = await client.get(ORDERS, headers=authorization(fresh_pair.access_token))
    assert fresh_history.status_code == 200
    assert fresh_history.json() == orders.json()
    assert PASSWORD not in json.dumps(registered) + login.text + refreshed.text


async def test_each_user_only_sees_own_data_before_and_after_refresh(client, db_session):
    first_pair = await register(client, "first@example.com")
    second_pair = await register(client, "second@example.com")
    first = await db_session.scalar(select(User).where(User.email == "first@example.com"))
    second = await db_session.scalar(select(User).where(User.email == "second@example.com"))
    first_order, first_subscription, first_code = await seed_account(db_session, first)
    second_order, second_subscription, second_code = await seed_account(db_session, second)
    for user, pair, own_order, own_subscription, own_code, foreign_user, foreign_code in (
        (first, first_pair, first_order, first_subscription, first_code, second, second_code),
        (second, second_pair, second_order, second_subscription, second_code, first, first_code),
    ):
        refreshed = await client.post(REFRESH, json={"refresh_token": pair["refresh_token"]})
        assert refreshed.status_code == 200
        for token in (pair["access_token"], refreshed.json()["access_token"]):
            assert tokens.decode_access_token(token) == user.id
            headers = authorization(token)
            orders = await client.get(ORDERS, params={"user_id": foreign_user.id}, headers=headers)
            assert orders.status_code == 200
            assert orders.json()["total"] == 1
            assert [item["id"] for item in orders.json()["orders"]] == [own_order.id]
            assert orders.json()["orders"][0]["cards"] == [{"nominal_usd": 10, "code": own_code}]
            assert foreign_code not in orders.text
            subscriptions = await client.get(SUBSCRIPTIONS, params={"user_id": foreign_user.id}, headers=headers)
            assert subscriptions.status_code == 200
            assert [item["id"] for item in subscriptions.json()["subscriptions"]] == [own_subscription.id]


@pytest.mark.parametrize("path", ME_PATHS)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}])
async def test_protected_routes_require_bearer(client, path, headers):
    response = await client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    ErrorResponse.model_validate(response.json())


@pytest.mark.parametrize("token", ["garbage", "a.b", "a.b.c", "a.b.c.d", ".."])
@pytest.mark.parametrize("path", ME_PATHS)
async def test_malformed_jwt_is_rejected(client, path, token):
    response = await client.get(path, headers=authorization(token))
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid token"}


@pytest.mark.parametrize("path", ME_PATHS)
@pytest.mark.parametrize("kind", ["wrong_signature", "unsigned", "wrong_algorithm", "expired", "future_iat", "refresh"])
async def test_untrusted_or_unusable_jwt_is_rejected(client, account, path, kind):
    if kind == "wrong_signature":
        token = signed_token(account.id, secret="another-signing-secret-at-least-32-bytes")
    elif kind == "unsigned":
        token = signed_token(account.id, secret="", algorithm="none")
    elif kind == "wrong_algorithm":
        token = signed_token(account.id, algorithm="HS512")
    elif kind == "expired":
        token = signed_token(account.id, exp=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif kind == "future_iat":
        token = signed_token(account.id, iat=datetime.now(timezone.utc) + timedelta(minutes=10))
    else:
        token = tokens.create_refresh_token(account.id)
    response = await client.get(path, headers=authorization(token))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    expected = "Token has expired" if kind == "expired" else "Invalid token"
    assert response.json() == {"detail": expected}


@pytest.mark.parametrize("path", ME_PATHS)
async def test_changed_subject_with_original_signature_is_rejected(client, db_session, account, path):
    other = User(email="other@example.com")
    db_session.add(other)
    await db_session.commit()
    token = tokens.create_access_token(account.id)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = str(other.id)
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    response = await client.get(path, headers=authorization(f"{header}.{forged_payload}.{signature}"))
    assert response.status_code == 401


@pytest.mark.parametrize("path", ME_PATHS)
@pytest.mark.parametrize("claims", [{"sub": "not-an-id"}, {"sub": "99999999"}, {"type": "unknown"}, {"sub": None}])
async def test_invalid_subject_or_type_is_rejected(client, account, path, claims):
    response = await client.get(path, headers=authorization(signed_token(account.id, **claims)))
    assert response.status_code == 401


@pytest.mark.parametrize("change", ["deactivate", "delete"])
async def test_token_cannot_access_or_refresh_after_account_disabled(client, db_session, account, change):
    access = tokens.create_access_token(account.id)
    refresh = tokens.create_refresh_token(account.id)
    if change == "deactivate":
        account.is_active = False
    else:
        await db_session.delete(account)
    await db_session.commit()
    for path in ME_PATHS:
        response = await client.get(path, headers=authorization(access))
        assert response.status_code == 401
        assert response.json() == {"detail": "User not found or inactive"}
    response = await client.post(REFRESH, json={"refresh_token": refresh})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid refresh token"}


@pytest.mark.parametrize("kind", ["garbage", "access", "expired", "wrong_signature", "unsigned"])
async def test_refresh_rejects_invalid_tokens(client, account, kind):
    if kind == "garbage":
        token = "not-a-jwt"
    elif kind == "access":
        token = tokens.create_access_token(account.id)
    elif kind == "expired":
        token = signed_token(account.id, token_type="refresh", exp=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif kind == "wrong_signature":
        token = signed_token(account.id, token_type="refresh", secret="another-signing-secret-at-least-32-bytes")
    else:
        token = signed_token(account.id, token_type="refresh", secret="", algorithm="none")
    response = await client.post(REFRESH, json={"refresh_token": token})
    assert response.status_code == 401
    expected = "Token has expired" if kind == "expired" else "Invalid refresh token"
    assert response.json() == {"detail": expected}


async def test_login_errors_do_not_disclose_email(client):
    await register(client)
    known = await client.post(LOGIN, json={"email": "buyer@example.com", "password": "WrongPass123"})
    unknown = await client.post(LOGIN, json={"email": "missing@example.com", "password": "WrongPass123"})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json() == {"detail": "Invalid email or password"}


async def test_duplicate_normalized_email_returns_documented_conflict(client):
    await register(client)
    response = await client.post(REGISTER, json={"email": "  BUYER@EXAMPLE.COM  ", "password": PASSWORD})
    assert response.status_code == 409
    assert ErrorResponse.model_validate(response.json()).detail == "User with this email already exists"


@pytest.mark.parametrize(
    "path,payload",
    [
        (REGISTER, {"email": "invalid", "password": PASSWORD}),
        (REGISTER, {"email": "buyer@example.com", "password": "short1"}),
        (REGISTER, {"email": "buyer@example.com", "password": "nodigitshere"}),
        (REGISTER, {"email": "buyer@example.com", "password": "12345678"}),
        (REGISTER, {"email": "buyer@example.com", "password": "Я" * 36 + "1"}),
        (LOGIN, {"email": "buyer@example.com"}),
        (REFRESH, {}),
        (REFRESH, {"refresh_token": ""}),
    ],
)
async def test_invalid_request_returns_validation_error(client, path, payload):
    response = await client.post(path, json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_pagination_boundaries_are_enforced(client, account, params):
    response = await client.get(ORDERS, params=params, headers=authorization(tokens.create_access_token(account.id)))
    assert response.status_code == 422


async def test_swagger_and_openapi_expose_bearer_and_all_auth_me_routes(client):
    docs = await client.get("/docs")
    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text
    assert "/openapi.json" in docs.text
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    bearer = schema["components"]["securitySchemes"]["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"
    assert bearer["description"]
    tag_descriptions = {tag["name"]: tag["description"] for tag in schema["tags"]}
    assert tag_descriptions["auth"] and tag_descriptions["me"]
    paths = {path for path in schema["paths"] if path.startswith(("/api/v1/auth/", "/api/v1/me/"))}
    assert paths == {REGISTER, LOGIN, REFRESH, ORDERS, SUBSCRIPTIONS}
    for path in paths:
        method = "get" if path in ME_PATHS else "post"
        operation = schema["paths"][path][method]
        assert operation["summary"] and operation["description"]
        assert operation["tags"] == ["me" if path in ME_PATHS else "auth"]
        if path in ME_PATHS:
            assert operation["security"] == [{"BearerAuth": []}]
        else:
            assert not operation.get("security")


@pytest.mark.parametrize(
    "path,method,status,model",
    [
        (REGISTER, "post", "201", TokenResponse),
        (LOGIN, "post", "200", TokenResponse),
        (REFRESH, "post", "200", TokenResponse),
        (ORDERS, "get", "200", OrderHistoryResponse),
        (SUBSCRIPTIONS, "get", "200", SubscriptionListResponse),
    ],
)
async def test_success_examples_match_actual_response_dtos(client, path, method, status, model):
    schema = (await client.get("/openapi.json")).json()
    content = schema["paths"][path][method]["responses"][status]["content"]["application/json"]
    assert content["schema"]["$ref"] == f"#/components/schemas/{model.__name__}"
    examples = [content["example"]] if "example" in content else [example["value"] for example in content["examples"].values()]
    assert examples
    for example in examples:
        model.model_validate(example)


async def test_error_schemas_headers_and_examples_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for path, method, statuses in (
        (REGISTER, "post", ("409",)),
        (LOGIN, "post", ("401",)),
        (REFRESH, "post", ("401",)),
        (ORDERS, "get", ("401", "403", "503")),
        (SUBSCRIPTIONS, "get", ("401", "403", "503")),
    ):
        responses = schema["paths"][path][method]["responses"]
        for status in statuses:
            response = responses[status]
            assert response["description"]
            content = response["content"]["application/json"]
            assert content["schema"]["$ref"] == "#/components/schemas/ErrorResponse"
            for example in content["examples"].values():
                ErrorResponse.model_validate(example["value"])
        if path in ME_PATHS or path == LOGIN:
            assert responses["401"]["headers"]["WWW-Authenticate"]["example"] == "Bearer"
    for path in (REGISTER, LOGIN, REFRESH, ORDERS):
        method = "get" if path == ORDERS else "post"
        response = schema["paths"][path][method]["responses"]["422"]
        assert response["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/HTTPValidationError"


async def test_request_fields_and_pagination_have_descriptions_and_examples(client):
    schema = (await client.get("/openapi.json")).json()
    for path, model in ((REGISTER, UserRegister), (LOGIN, UserLogin), (REFRESH, RefreshRequest)):
        request = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert request["$ref"] == f"#/components/schemas/{model.__name__}"
        fields = schema["components"]["schemas"][model.__name__]["properties"]
        example = {name: field["examples"][0] for name, field in fields.items()}
        model.model_validate(example)
        assert all(field["description"] for field in fields.values())
        if "password" in fields:
            assert fields["password"]["writeOnly"] is True
            assert fields["password"]["format"] == "password"
    parameters = {parameter["name"]: parameter for parameter in schema["paths"][ORDERS]["get"]["parameters"]}
    assert parameters["limit"]["description"] and parameters["offset"]["description"]
    assert parameters["limit"]["schema"]["default"] == 20
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["offset"]["schema"]["minimum"] == 0
