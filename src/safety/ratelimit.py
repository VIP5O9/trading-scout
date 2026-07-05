"""Daily LLM call budget. Checked BEFORE every LLM dispatch; when the cap is hit
the user gets a clear message and scans refuse until the (UTC) day rolls over.
Tracked separately from the Finnhub news cap (different `service` key)."""
from __future__ import annotations

SERVICE_LLM = "llm"


class LLMBudgetExceeded(Exception):
    def __init__(self, used: int, cap: int):
        super().__init__(
            f"Daily LLM budget reached ({used}/{cap} calls used). "
            "Scans and briefs are paused until tomorrow (UTC). You can raise "
            "max_llm_calls_per_day by redoing /interview if this is too tight.")
        self.used = used
        self.cap = cap


async def require_llm_budget(db, cap: int) -> None:
    """Raise LLMBudgetExceeded if the day's budget is spent. Call before dispatch."""
    used = await db.count_api_calls_today(SERVICE_LLM)
    if used >= cap:
        raise LLMBudgetExceeded(used, cap)


async def spend_llm_call(db) -> None:
    await db.log_api_call(SERVICE_LLM)
