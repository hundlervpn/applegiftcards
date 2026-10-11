"""Seed dev gift cards ($10, $20, $50). Run: .venv/bin/python scripts/seed_data.py"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.db.session import dispose_engine, get_session_maker
from app.models.gift_card import GiftCard
from app.models.price import Price

SEED = (
    (10, Decimal("999.00"), "TEST-10USD-AAAA-BBBB-0001"),
    (20, Decimal("1999.00"), "TEST-20USD-CCCC-DDDD-0001"),
    (50, Decimal("4999.00"), "TEST-50USD-EEEE-FFFF-0001"),
)


async def main() -> None:
    try:
        async with get_session_maker()() as session:
            if await session.scalar(select(func.count()).select_from(GiftCard)):
                print("gift_cards is not empty; seed skipped")
                return

            for nominal_usd, price_rub, _code in SEED:
                if await session.get(Price, nominal_usd) is None:
                    session.add(Price(nominal_usd=nominal_usd, price_rub=price_rub))
            await session.flush()

            for nominal_usd, _price_rub, code in SEED:
                session.add(
                    GiftCard(
                        nominal_usd=nominal_usd,
                        code_encrypted=code.encode("utf-8"),
                    )
                )
            await session.commit()

        print(f"Seeded {len(SEED)} gift cards.")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
