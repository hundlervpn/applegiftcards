import asyncio
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — импорт регистрирует все таблицы в Base.metadata
from app.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app

TEST_DATABASE_NAME = "awe_test"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    server_url, _, _ = get_settings().database_url.rpartition("/")
    return f"{server_url}/{TEST_DATABASE_NAME}"


@pytest.fixture(scope="session")
def prepare_test_database(test_database_url: str) -> None:
    """Создаёт чистую базу awe_test рядом с dev-базой, данные dev-базы не трогает."""

    async def _prepare() -> None:
        server_url, _, _ = get_settings().database_url.rpartition("/")
        engine = create_async_engine(f"{server_url}/postgres")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DATABASE_NAME},
            )
        if not exists:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
        await engine.dispose()

    asyncio.run(_prepare())


@pytest_asyncio.fixture
async def db_engine(prepare_test_database, test_database_url: str) -> AsyncGenerator[object, None]:
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine) -> AsyncGenerator[AsyncClient, None]:
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_maker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _do_not_run_billing_scheduler(monkeypatch):
    """TestClient входит в lifespan. Настоящий планировщик списал бы подписки из dev-базы."""

    async def idle() -> None:
        return None

    monkeypatch.setattr("app.main.run_billing_scheduler", idle)
