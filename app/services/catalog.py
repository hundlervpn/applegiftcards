from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import catalog as catalog_repo
from app.schemas.catalog import CatalogItemResponse


async def get_catalog(session: AsyncSession) -> list[CatalogItemResponse]:
    rows = await catalog_repo.list_catalog_rows(session)
    return [
        CatalogItemResponse(
            nominal_usd=nominal_usd,
            price_rub=price_rub,
            available_count=available_count,
        )
        for nominal_usd, price_rub, available_count in rows
    ]
