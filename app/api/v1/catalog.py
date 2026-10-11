from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.catalog import CatalogItemResponse
from app.services.catalog import get_catalog

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("", response_model=list[CatalogItemResponse])
async def read_catalog(
    session: AsyncSession = Depends(get_db),
) -> list[CatalogItemResponse]:
    return await get_catalog(session)
