from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.order import OneTimeOrderCreate, OneTimeOrderResponse
from app.services.inventory import GiftCardUnavailableError
from app.services.order_checkout import create_one_time_order_checkout
from app.services.orders import NominalNotForSaleError
from app.services.payments import (
    AcquirerBadResponseError,
    AcquirerConfigurationError,
    AcquirerUnavailableError,
)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/one-time", status_code=201, response_model=OneTimeOrderResponse)
async def create_one_time_order(
    body: OneTimeOrderCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OneTimeOrderResponse:
    try:
        checkout = await create_one_time_order_checkout(
            db,
            email=str(body.email),
            nominal_usd=body.nominal_usd,
            settings=settings,
            is_subscription=body.is_subscription,
        )
    except NominalNotForSaleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GiftCardUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail="No gift cards are available for this nominal",
        ) from exc
    except AcquirerConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Payment provider is not configured",
        ) from exc
    except AcquirerUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Payment provider is unavailable",
        ) from exc
    except AcquirerBadResponseError as exc:
        raise HTTPException(
            status_code=502,
            detail="Payment provider returned an invalid response",
        ) from exc
    return OneTimeOrderResponse(
        order_id=checkout.order_id,
        payment_url=checkout.payment_url,
    )
