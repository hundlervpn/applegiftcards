import importlib
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db.session import get_db
from app.services.order_checkout import (
    OneTimeOrderCheckout,
    create_one_time_order_checkout,
)
from app.services.payments import (
    AcquirerBadResponseError,
    AcquirerConfigurationError,
    AcquirerUnavailableError,
    PaymentSession,
    create_payment,
)

with patch.dict(
    os.environ,
    {"DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:5432/test"},
):
    create_app = importlib.import_module("app.main").create_app


class FakeSession:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class OrderPaymentEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = FakeSession()
        self.settings = Settings(
            database_url="postgresql+asyncpg://test:test@127.0.0.1:5432/test",
            yookassa_shop_id="test-shop",
            yookassa_secret_key="test-secret",
            payment_return_url="https://example.test/payment/complete",
        )
        self.app = create_app()

        async def override_db():
            yield self.session

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_settings] = lambda: self.settings
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()

    def test_order_response_includes_yookassa_payment_url(self) -> None:
        with patch(
            "app.api.v1.orders.create_one_time_order_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout:
            mock_checkout.return_value = OneTimeOrderCheckout(
                order_id=42,
                payment_url="https://yookassa.test/pay/123",
            )
            response = self.client.post(
                "/api/v1/orders/one-time",
                json={"email": "buyer@example.com", "nominal_usd": 10},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "order_id": 42,
                "payment_url": "https://yookassa.test/pay/123",
            },
        )
        mock_checkout.assert_awaited_once()

    def test_unavailable_yookassa_maps_to_service_unavailable(self) -> None:
        with patch(
            "app.api.v1.orders.create_one_time_order_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout:
            mock_checkout.side_effect = AcquirerUnavailableError("provider unavailable")
            response = self.client.post(
                "/api/v1/orders/one-time",
                json={"email": "buyer@example.com", "nominal_usd": 10},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Payment provider is unavailable")

    def test_invalid_yookassa_response_maps_to_bad_gateway(self) -> None:
        with patch(
            "app.api.v1.orders.create_one_time_order_checkout",
            new_callable=AsyncMock,
        ) as mock_checkout:
            mock_checkout.side_effect = AcquirerBadResponseError("invalid provider response")
            response = self.client.post(
                "/api/v1/orders/one-time",
                json={"email": "buyer@example.com", "nominal_usd": 10},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "Payment provider returned an invalid response",
        )


class OrderCheckoutServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = FakeSession()
        self.settings = Settings(
            database_url="postgresql+asyncpg://test:test@127.0.0.1:5432/test",
            yookassa_shop_id="test-shop",
            yookassa_secret_key="test-secret",
            payment_return_url="https://example.test/payment/complete",
        )
        self.order = SimpleNamespace(
            id=42,
            total_amount_rub=Decimal("999.00"),
        )

    async def test_order_and_payment_are_committed_and_returned(self) -> None:
        with (
            patch(
                "app.services.order_checkout.create_one_time_order",
                new_callable=AsyncMock,
            ) as mock_create_order,
            patch(
                "app.services.order_checkout.create_payment",
                new_callable=AsyncMock,
            ) as mock_create_payment,
        ):
            mock_create_order.return_value = self.order
            mock_create_payment.return_value = PaymentSession(
                payment_url="https://yookassa.test/pay/123",
                external_payment_id="payment-123",
            )

            checkout = await create_one_time_order_checkout(
                self.session,
                email="buyer@example.com",
                nominal_usd=10,
                settings=self.settings,
            )

        self.assertEqual(checkout.order_id, 42)
        self.assertEqual(checkout.payment_url, "https://yookassa.test/pay/123")
        mock_create_payment.assert_awaited_once_with(
            shop_id="test-shop",
            secret_key="test-secret",
            order_id=42,
            amount_rub=Decimal("999.00"),
            return_url="https://example.test/payment/complete",
        )
        self.session.commit.assert_awaited_once()
        self.session.rollback.assert_not_awaited()

    async def test_payment_failure_rolls_back_order_and_reservation(self) -> None:
        with (
            patch(
                "app.services.order_checkout.create_one_time_order",
                new_callable=AsyncMock,
            ) as mock_create_order,
            patch(
                "app.services.order_checkout.create_payment",
                new_callable=AsyncMock,
            ) as mock_create_payment,
        ):
            mock_create_order.return_value = self.order
            mock_create_payment.side_effect = AcquirerUnavailableError("offline")

            with self.assertRaises(AcquirerUnavailableError):
                await create_one_time_order_checkout(
                    self.session,
                    email="buyer@example.com",
                    nominal_usd=10,
                    settings=self.settings,
                )

        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()


class PaymentConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_credentials_fail_before_network_request(self) -> None:
        with patch("app.services.payments.httpx.AsyncClient") as mock_http_client:
            with self.assertRaises(AcquirerConfigurationError):
                await create_payment(
                    shop_id="",
                    secret_key="",
                    order_id=42,
                    amount_rub=Decimal("999.00"),
                    return_url="https://example.test/payment/complete",
                )

        mock_http_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
