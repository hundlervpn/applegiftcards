from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.order import OneTimeOrderCreate, OneTimeOrderResponse
from app.services.inventory import GiftCardUnavailableError
from app.services.orders import NominalNotForSaleError, create_one_time_order as create_one_time_order_record

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/one-time", status_code=201, response_model=OneTimeOrderResponse)
async def create_one_time_order(
    body: OneTimeOrderCreate,
    db: AsyncSession = Depends(get_db),
) -> OneTimeOrderResponse:
    try:
        order_id = await create_one_time_order_record(
            db,
            email=str(body.email),
            nominal_usd=body.nominal_usd,
        )
    except NominalNotForSaleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GiftCardUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail="No gift cards are available for this nominal",
        ) from exc
    return OneTimeOrderResponse(order_id=order_id)
