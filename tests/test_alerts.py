import logging
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.services.alerts import (
    build_low_stock_message,
    find_low_stock,
    notify_low_stock,
)


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
        "telegram_bot_token": "test-bot-token",
        "telegram_admin_chat_id": "12345",
        "low_stock_threshold": 3,
    }
    values.update(overrides)
    return Settings(**values)


class FakeTelegramClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.error = error

    async def post(self, url: str, json: dict | None = None):
        self.posts.append((url, json))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(raise_for_status=lambda: None)


class BuildLowStockMessageTests(unittest.TestCase):
    def test_message_lists_each_low_nominal(self) -> None:
        text = build_low_stock_message([(10, 2), (20, 0)])

        self.assertIn("$10", text)
        self.assertIn("2", text)
        self.assertIn("$20", text)
        self.assertIn("0", text)
        self.assertIn("Заканчиваются", text)


class FindLowStockTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_nominals_below_threshold(self) -> None:
        rows = [(10, Decimal("999.00"), 2), (20, Decimal("1999.00"), 5), (50, Decimal("4999.00"), 0)]
        session = SimpleNamespace()
        with (
            patch("app.services.alerts.get_settings", return_value=make_settings()),
            patch(
                "app.services.alerts.catalog_repo.list_catalog_rows",
                new=AsyncMock(return_value=rows),
            ),
        ):
            alerts = await find_low_stock(session)

        self.assertEqual(alerts, [(10, 2), (50, 0)])


class NotifyLowStockTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_single_message_with_low_nominals(self) -> None:
        rows = [(10, Decimal("999.00"), 2), (20, Decimal("1999.00"), 7)]
        client = FakeTelegramClient()
        session = SimpleNamespace()
        with (
            patch("app.services.alerts.get_settings", return_value=make_settings()),
            patch(
                "app.services.alerts.catalog_repo.list_catalog_rows",
                new=AsyncMock(return_value=rows),
            ),
        ):
            await notify_low_stock(session, client=client)

        self.assertEqual(len(client.posts), 1)
        url, payload = client.posts[0]
        self.assertIn("/bottest-bot-token/sendMessage", url)
        self.assertEqual(payload["chat_id"], "12345")
        self.assertIn("$10", payload["text"])
        self.assertNotIn("$20", payload["text"])

    async def test_skips_when_all_nominals_are_stocked(self) -> None:
        rows = [(10, Decimal("999.00"), 10), (20, Decimal("1999.00"), 4)]
        client = FakeTelegramClient()
        session = SimpleNamespace()
        with (
            patch("app.services.alerts.get_settings", return_value=make_settings()),
            patch(
                "app.services.alerts.catalog_repo.list_catalog_rows",
                new=AsyncMock(return_value=rows),
            ),
        ):
            await notify_low_stock(session, client=client)

        self.assertEqual(client.posts, [])

    async def test_skips_when_telegram_is_not_configured(self) -> None:
        client = FakeTelegramClient()
        session = SimpleNamespace()
        with (
            patch(
                "app.services.alerts.get_settings",
                return_value=make_settings(telegram_bot_token="", telegram_admin_chat_id=""),
            ),
            patch(
                "app.services.alerts.catalog_repo.list_catalog_rows",
                new=AsyncMock(return_value=[(10, Decimal("999.00"), 1)]),
            ),
        ):
            await notify_low_stock(session, client=client)

        self.assertEqual(client.posts, [])

    async def test_send_error_is_swallowed_and_logged(self) -> None:
        rows = [(10, Decimal("999.00"), 1)]
        client = FakeTelegramClient(error=httpx.ConnectError("boom"))
        session = SimpleNamespace()
        with (
            patch("app.services.alerts.get_settings", return_value=make_settings()),
            patch(
                "app.services.alerts.catalog_repo.list_catalog_rows",
                new=AsyncMock(return_value=rows),
            ),
            self.assertLogs("app.services.alerts", level=logging.WARNING) as logs,
        ):
            await notify_low_stock(session, client=client)

        self.assertEqual(len(client.posts), 1)
        self.assertTrue(any("low stock" in message for message in logs.output))
