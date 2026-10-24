from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.me import router as me_router
from app.api.v1.payment_webhook import router as payment_webhook_router
from app.api.v1.orders import router as orders_router
from app.config import get_settings
from app.db.session import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(payment_webhook_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    return app


app = create_app()
