"""Market intel / premarket brief: an LLM-written NARRATIVE over data this app
already fetched (watchlist metrics + sector momentum). Always labeled as
interpretation — unlike proposals, a brief never claims a rule fired and can
never lead to an order."""
from __future__ import annotations

import json

from src.intel.sector import sector_momentum
from src.modes.running import fetch_symbol_metrics
from src.safety.ratelimit import require_llm_budget, spend_llm_call

BRIEF_SYSTEM_SUFFIX = """
MODE: RUNNING (intel brief). Write a short plain-English summary of the data
below for a non-trader: what moved on their watchlist, which sectors lead/lag.
Rules:
- Only cite numbers present in the data. Never estimate or recall anything.
- 8 sentences maximum. No advice, no "you should", no predictions.
- Do not mention rules or proposals — this is a weather report, not a signal.
"""

LABEL = ("🧭 <i>This is an LLM summary/interpretation of live data — "
         "not a rule match, not advice.</i>\n\n")


async def market_brief(db, llm, broker, engine_prompt: str,
                       watchlist: list[str], llm_cap: int) -> str:
    await require_llm_budget(db, llm_cap)
    moves = {}
    for sym in watchlist[:10]:
        try:
            snap = await fetch_symbol_metrics(broker, sym)
            m = snap["metrics"]
            moves[sym] = {k: m[k] for k in
                          ("price", "pct_change_1d", "pct_from_5d_high")
                          if k in m}
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "NeedsAuth":
                raise
            moves[sym] = {"error": str(exc)[:120]}
    sectors = await sector_momentum(broker)
    await spend_llm_call(db)
    text = await llm.complete(
        engine_prompt + BRIEF_SYSTEM_SUFFIX,
        [{"role": "user", "content": json.dumps(
            {"WATCHLIST_MOVES": moves, "SECTOR_MOMENTUM_20D": sectors})}],
        max_tokens=600)
    return LABEL + text.strip()
