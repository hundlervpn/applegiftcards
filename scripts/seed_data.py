"""Seed dev gift cards: 4 available cards for every allowed nominal.

Run: .venv/bin/python scripts/seed_data.py
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.db.session import dispose_engine, get_session_maker
from app.models.gift_card import GiftCard
from app.models.price import Price

CARDS_PER_NOMINAL = 4

# Same set as the prices.nominal_usd check.
NOMINALS_USD = (1, 2, 3, 4, 5, 10, 15, 20, 25, 30, 40, 50, 100, 200)


def _price_rub(nominal_usd: int) -> Decimal:
    # Dev prices follow $10 → 999, $20 → 1999, $50 → 4999.
    return Decimal(nominal_usd * 100 - 1)


def _codes(nominal_usd: int) -> tuple[str, ...]:
    return tuple(
        f"TEST-{nominal_usd}USD-{index:04d}"
        for index in range(1, CARDS_PER_NOMINAL + 1)
    )


async def main() -> None:
    try:
        async with get_session_maker()() as session:
            if await session.scalar(select(func.count()).select_from(GiftCard)):
                print("gift_cards is not empty; seed skipped")
                return

            for nominal_usd in NOMINALS_USD:
                if await session.get(Price, nominal_usd) is None:
                    session.add(
                        Price(nominal_usd=nominal_usd, price_rub=_price_rub(nominal_usd))
                    )
            await session.flush()

            card_count = 0
            for nominal_usd in NOMINALS_USD:
                for code in _codes(nominal_usd):
                    session.add(
                        GiftCard(
                            nominal_usd=nominal_usd,
                            code_encrypted=code.encode("utf-8"),
                        )
                    )
                    card_count += 1
            await session.commit()

        print(f"Seeded {card_count} gift cards ({CARDS_PER_NOMINAL} per nominal).")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
