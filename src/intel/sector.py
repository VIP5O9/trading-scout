from __future__ import annotations

import asyncio
from typing import Any

SECTOR_ETFS = ["XLK", "XLE", "XLV", "XLF", "XLY", "XBI", "XLC", "XLI"]


async def sector_momentum(broker: Any) -> list[dict]:
    """Rank sector ETFs by 20-trading-day return_pct (desc)."""

    async def fetch(symbol: str) -> dict:
        try:
            data = await broker.get_historicals(
                symbol, interval="day", span="3month"
            )
            if len(data) < 21:
                return {"symbol": symbol, "error": "insufficient data"}
            close_21 = data[-21]["close"]
            last_close = data[-1]["close"]
            return_pct = round((last_close / close_21 - 1) * 100, 2)
            return {
                "symbol": symbol,
                "last_close": last_close,
                "return_20d_pct": return_pct,
            }
        except Exception as exc:
            if type(exc).__name__ == "NeedsAuth":
                raise  # broker login required — surface it, don't bury it
            return {"symbol": symbol, "error": str(exc)}

    results = await asyncio.gather(*[fetch(s) for s in SECTOR_ETFS])
    ranked = [r for r in results if "return_20d_pct" in r]
    errors = [r for r in results if "error" in r]
    ranked.sort(key=lambda x: x["return_20d_pct"], reverse=True)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return ranked + errors
