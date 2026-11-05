import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "awe_support_migration"


def alembic(url, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_support_migration_upgrade_downgrade_preserves_financial_history():
    server, _, _ = get_settings().database_url.rpartition("/")
    url = server + "/" + DATABASE
    admin = create_async_engine(server + "/postgres", isolation_level="AUTOCOMMIT")
    engine = create_async_engine(url)
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{DATABASE}"'))
        alembic(url, "upgrade", "0004_subscriptions")
        async with engine.begin() as conn:
            user_id = await conn.scalar(
                text(
                    "INSERT INTO users (email) VALUES ('support-migration@example.com') RETURNING id"
                )
            )
            order_id = await conn.scalar(
                text(
                    "INSERT INTO orders (user_id, items, total_amount_rub) "
                    "VALUES (:user_id, CAST(:items AS jsonb), 999.00) RETURNING id"
                ),
                {"user_id": user_id, "items": '[{"nominal_usd": 10}]'},
            )
        alembic(url, "upgrade", "head")
        async with engine.begin() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0005_support_tickets"
            columns = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type, character_maximum_length, is_nullable, is_identity FROM information_schema.columns WHERE table_name = 'support_tickets' ORDER BY ordinal_position"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert [row["column_name"] for row in columns] == [
                "id",
                "user_id",
                "order_id",
                "subject",
                "message",
                "status",
                "created_at",
            ]
            assert columns[0]["data_type"] == "bigint" and columns[0]["is_identity"] == "YES"
            assert columns[1]["is_nullable"] == "NO"
            assert columns[2]["is_nullable"] == "YES"
            assert columns[3]["character_maximum_length"] == 120
            assert columns[4]["data_type"] == "text"
            assert columns[6]["data_type"] == "timestamp with time zone"
            labels = list(
                await conn.scalars(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='support_ticket_status' ORDER BY e.enumsortorder"
                    )
                )
            )
            assert labels == ["open", "in_progress", "resolved", "closed"]
            indexes = list(
                await conn.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename='support_tickets'")
                )
            )
            assert {"ix_support_tickets_user_created", "ix_support_tickets_order_id"} <= set(
                indexes
            )
            ticket = (
                (
                    await conn.execute(
                        text(
                            "INSERT INTO support_tickets (user_id, order_id, subject, message) VALUES (:user_id, :order_id, 'Invalid code', 'The code cannot be activated') RETURNING id, status, created_at"
                        ),
                        {"user_id": user_id, "order_id": order_id},
                    )
                )
                .mappings()
                .one()
            )
            assert ticket["id"] > 0 and ticket["status"] == "open"
            assert ticket["created_at"].tzinfo is not None
            general = await conn.scalar(
                text(
                    "INSERT INTO support_tickets (user_id, subject, message) VALUES (:user_id, 'General question', 'A question without an order') RETURNING order_id"
                ),
                {"user_id": user_id},
            )
            assert general is None
        invalid = [
            (
                {
                    "user_id": user_id + 999,
                    "order_id": order_id,
                    "subject": "Valid subject",
                    "message": "Valid message for test",
                }
            ),
            (
                {
                    "user_id": user_id,
                    "order_id": order_id + 999,
                    "subject": "Valid subject",
                    "message": "Valid message for test",
                }
            ),
            (
                {
                    "user_id": user_id,
                    "order_id": None,
                    "subject": "bad",
                    "message": "Valid message for test",
                }
            ),
            (
                {
                    "user_id": user_id,
                    "order_id": None,
                    "subject": "Valid subject",
                    "message": "short",
                }
            ),
            (
                {
                    "user_id": user_id,
                    "order_id": None,
                    "subject": "Valid subject",
                    "message": "m" * 2001,
                }
            ),
        ]
        for values in invalid:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO support_tickets (user_id, order_id, subject, message) VALUES (:user_id, :order_id, :subject, :message)"
                        ),
                        values,
                    )
        for sql, params in [
            ("DELETE FROM users WHERE id=:id", {"id": user_id}),
            ("DELETE FROM orders WHERE id=:id", {"id": order_id}),
        ]:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(text(sql), params)
        alembic(url, "downgrade", "0004_subscriptions")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('support_tickets')")) is None
            assert (
                await conn.scalar(
                    text("SELECT 1 FROM pg_type WHERE typname='support_ticket_status'")
                )
                is None
            )
            assert (
                await conn.scalar(text("SELECT count(*) FROM users WHERE id=:id"), {"id": user_id})
                == 1
            )
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM orders WHERE id=:id"), {"id": order_id}
                )
                == 1
            )
            assert await conn.scalar(text("SELECT to_regclass('subscriptions')")) == "subscriptions"
        alembic(url, "upgrade", "head")
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
        await admin.dispose()
