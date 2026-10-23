import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DB = "awe_subscriptions_migration"
HEAD_REVISION = "0004_subscriptions"
PREVIOUS_REVISION = "0003_scrum34"


def _database_url(name: str) -> str:
    server_url, _, _ = get_settings().database_url.rpartition("/")
    return f"{server_url}/{name}"


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    return completed


async def _admin_execute(sql: str) -> None:
    server_url, _, _ = get_settings().database_url.rpartition("/")
    engine = create_async_engine(f"{server_url}/postgres")
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(sql))
    await engine.dispose()


async def _recreate_database() -> None:
    await _admin_execute(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)')
    await _admin_execute(f'CREATE DATABASE "{MIGRATION_DB}"')


async def _drop_database() -> None:
    await _admin_execute(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)')


async def _column_rows(url: str) -> list[dict]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT column_name, data_type, udt_name, is_nullable,
                       character_maximum_length, is_identity, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'subscriptions'
                ORDER BY ordinal_position
                """
            )
        )
        rows = [dict(row) for row in result.mappings()]
    await engine.dispose()
    return rows


async def _constraint_defs(url: str) -> str:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid = 'public.subscriptions'::regclass
                ORDER BY conname
                """
            )
        )
        definitions = "\n".join(row.definition for row in result)
    await engine.dispose()
    return definitions


async def _index_definitions(url: str) -> dict[str, str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'subscriptions'
                """
            )
        )
        indexes = {row.indexname: row.indexdef for row in result}
    await engine.dispose()
    return indexes


async def _enum_labels(url: str) -> list[str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT e.enumlabel
                FROM pg_type t
                JOIN pg_enum e ON t.oid = e.enumtypid
                WHERE t.typname = 'subscription_status'
                ORDER BY e.enumsortorder
                """
            )
        )
        labels = [row.enumlabel for row in result]
    await engine.dispose()
    return labels


async def _assert_upgraded_schema(url: str) -> None:
    current = _alembic("current", database_url=url).stdout
    assert HEAD_REVISION in current

    columns = {row["column_name"]: row for row in await _column_rows(url)}
    assert list(columns) == [
        "id",
        "user_id",
        "nominal_usd",
        "payment_method_id",
        "status",
        "next_billing_at",
        "dunning_attempts",
        "created_at",
        "updated_at",
    ]
    assert columns["id"]["is_identity"] == "YES"
    assert columns["id"]["data_type"] == "bigint"
    assert columns["user_id"]["data_type"] == "bigint"
    assert columns["nominal_usd"]["data_type"] == "smallint"
    assert columns["payment_method_id"]["data_type"] == "character varying"
    assert columns["payment_method_id"]["character_maximum_length"] == 64
    assert columns["status"]["udt_name"] == "subscription_status"
    assert "'active'::subscription_status" in columns["status"]["column_default"]
    assert columns["next_billing_at"]["data_type"] == "timestamp with time zone"
    assert columns["dunning_attempts"]["data_type"] == "smallint"
    assert columns["dunning_attempts"]["column_default"] == "0"
    assert columns["created_at"]["data_type"] == "timestamp with time zone"
    assert "now()" in columns["created_at"]["column_default"]
    assert columns["updated_at"]["data_type"] == "timestamp with time zone"
    assert "now()" in columns["updated_at"]["column_default"]
    assert all(row["is_nullable"] == "NO" for row in columns.values())

    assert await _enum_labels(url) == ["active", "past_due", "cancelled"]

    definitions = await _constraint_defs(url)
    assert "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT DEFERRABLE" in definitions
    assert "FOREIGN KEY (nominal_usd) REFERENCES prices(nominal_usd) ON DELETE RESTRICT DEFERRABLE" in definitions
    assert "dunning_attempts >= 0" in definitions
    assert "btrim" in definitions and "payment_method_id" in definitions
    await _assert_foreign_keys_are_initially_immediate(url)

    indexes = await _index_definitions(url)
    assert "ix_subscriptions_status_next_billing_at" in indexes
    assert "(status, next_billing_at)" in indexes["ix_subscriptions_status_next_billing_at"]
    assert "ix_subscriptions_user_id" in indexes
    assert "(user_id)" in indexes["ix_subscriptions_user_id"]


async def _assert_foreign_keys_are_initially_immediate(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT conname, condeferrable, condeferred
                    FROM pg_constraint
                    WHERE conrelid = 'public.subscriptions'::regclass
                      AND contype = 'f'
                    """
                )
            )
        ).all()
    await engine.dispose()
    assert len(rows) == 2
    assert all(row.condeferrable and not row.condeferred for row in rows)


async def _assert_integrity(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        user_id = await conn.scalar(
            text("INSERT INTO users (email) VALUES ('migration-sub@example.com') RETURNING id")
        )
        await conn.execute(text("INSERT INTO prices (nominal_usd, price_rub) VALUES (10, 1500.00)"))
        status, dunning_attempts = (
            await conn.execute(
                text(
                    """
                    INSERT INTO subscriptions (
                        user_id, nominal_usd, payment_method_id, next_billing_at
                    )
                    VALUES (
                        :user_id, 10, :payment_method_id, timestamptz '2026-10-23 12:00:00+00'
                    )
                    RETURNING status::text, dunning_attempts
                    """
                ),
                {"user_id": user_id, "payment_method_id": "p" * 64},
            )
        ).one()
    assert status == "active"
    assert dunning_attempts == 0

    invalid = [
        (
            """
            INSERT INTO subscriptions (user_id, nominal_usd, payment_method_id, next_billing_at)
            VALUES (999999, 10, 'pm', timestamptz '2026-10-23 12:00:00+00')
            """,
            {},
        ),
        (
            """
            INSERT INTO subscriptions (user_id, nominal_usd, payment_method_id, next_billing_at)
            VALUES (:user_id, 15, 'pm', timestamptz '2026-10-23 12:00:00+00')
            """,
            {"user_id": user_id},
        ),
        (
            """
            INSERT INTO subscriptions (
                user_id, nominal_usd, payment_method_id, next_billing_at, dunning_attempts
            )
            VALUES (:user_id, 10, 'pm', timestamptz '2026-10-23 12:00:00+00', -1)
            """,
            {"user_id": user_id},
        ),
        (
            """
            INSERT INTO subscriptions (user_id, nominal_usd, payment_method_id, next_billing_at)
            VALUES (:user_id, 10, '   ', timestamptz '2026-10-23 12:00:00+00')
            """,
            {"user_id": user_id},
        ),
    ]
    for sql, params in invalid:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)

    with pytest.raises(DBAPIError, match="value too long"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO subscriptions (user_id, nominal_usd, payment_method_id, next_billing_at)
                    VALUES (:user_id, 10, :payment_method_id, timestamptz '2026-10-23 12:00:00+00')
                    """
                ),
                {"user_id": user_id, "payment_method_id": "p" * 65},
            )
    await engine.dispose()


async def _assert_downgraded(url: str) -> None:
    current = _alembic("current", database_url=url).stdout
    assert PREVIOUS_REVISION in current
    assert HEAD_REVISION not in current

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        subscriptions = await conn.scalar(text("SELECT to_regclass('public.subscriptions')"))
        enum_exists = await conn.scalar(
            text("SELECT 1 FROM pg_type WHERE typname = 'subscription_status'")
        )
        users = await conn.scalar(text("SELECT to_regclass('public.users')"))
    await engine.dispose()
    assert subscriptions is None
    assert enum_exists is None
    assert users == "users"


async def test_alembic_upgrade_head_and_downgrade_on_postgres():
    url = _database_url(MIGRATION_DB)
    await _recreate_database()
    try:
        _alembic("upgrade", "head", database_url=url)
        await _assert_upgraded_schema(url)
        await _assert_integrity(url)

        _alembic("downgrade", "-1", database_url=url)
        await _assert_downgraded(url)

        _alembic("upgrade", "head", database_url=url)
        await _assert_upgraded_schema(url)
    finally:
        await _drop_database()
