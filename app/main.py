import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.openapi import API_TAGS
from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.me import router as me_router
from app.api.v1.me_subscriptions import router as me_subscriptions_router
from app.api.v1.payment_webhook import router as payment_webhook_router
from app.api.v1.orders import router as orders_router
from app.api.v1.subscriptions import router as subscriptions_router
from app.config import get_settings
from app.db.session import dispose_engine
from app.services.scheduler import run_billing_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = asyncio.create_task(run_billing_scheduler(), name="billing-scheduler")
    try:
        yield
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Billing scheduler stopped with an error")
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        openapi_tags=API_TAGS,
    )
    app.include_router(health_router)
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(payment_webhook_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.include_router(me_subscriptions_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")
    return app


app = create_app()
