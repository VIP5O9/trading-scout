"""RUNNING mode: user taps /scan -> live data -> rule check -> proposal cards.

Trust model, in order:
1. Metrics are computed HERE from live broker data — never by the LLM.
2. The LLM (via the validated-JSON fallback) may only claim a rule fired; every
   claimed condition_check is re-evaluated numerically against our own computed
   metrics, and anything that doesn't actually hold is discarded.
3. Quantity is re-derived here from account equity and max_position_pct — the
   LLM's qty is capped, never trusted.
4. Immediately before any order (see main.py), verify_condition_fresh() re-fetches
   live data and re-evaluates the ORIGINAL metric/op/threshold — not the LLM's
   restated sentence.

This module never places orders and never imports the order-placement function.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from src.llm.fallback import complete_json
from src.safety.errors import with_retry
from src.safety.ratelimit import require_llm_budget, spend_llm_call

ALLOWED_METRICS = (
    "price", "pct_change_1d", "pct_from_5d_high", "pct_from_20d_high",
    "sma20", "sma50", "price_vs_sma20_pct", "price_vs_sma50_pct",
    "sma50_slope_5d_pct",
)
ALLOWED_OPS = (">=", "<=", ">", "<")


class ConditionCheck(BaseModel):
    metric: str
    op: str
    value: float


class ProposalDraft(BaseModel):
    action: str                      # BUY | SELL
    symbol: str
    qty: int = Field(ge=1)
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    matched_condition: str           # exact rule + exact numbers, plain English
    condition_check: ConditionCheck


class ScanResult(BaseModel):
    proposals: list[ProposalDraft] = Field(default_factory=list)
    no_match_note: str | None = None


# ------------------------------------------------------------------ metrics --

def compute_metrics(closes: list[float], live_price: float,
                    previous_close: float | None) -> dict[str, float]:
    """All metrics the strategy language may reference. closes are daily closes,
    chronological ascending, NOT including today's live price."""
    m: dict[str, float] = {"price": round(live_price, 4)}
    if previous_close:
        m["pct_change_1d"] = round((live_price / previous_close - 1) * 100, 2)
    if len(closes) >= 5:
        high5 = max(closes[-5:] + [live_price])
        m["pct_from_5d_high"] = round((1 - live_price / high5) * 100, 2)
    if len(closes) >= 20:
        high20 = max(closes[-20:] + [live_price])
        m["pct_from_20d_high"] = round((1 - live_price / high20) * 100, 2)
        sma20 = sum(closes[-20:]) / 20
        m["sma20"] = round(sma20, 4)
        m["price_vs_sma20_pct"] = round((live_price / sma20 - 1) * 100, 2)
    if len(closes) >= 50:
        sma50 = sum(closes[-50:]) / 50
        m["sma50"] = round(sma50, 4)
        m["price_vs_sma50_pct"] = round((live_price / sma50 - 1) * 100, 2)
        if len(closes) >= 55:
            sma50_prev = sum(closes[-55:-5]) / 50
            m["sma50_slope_5d_pct"] = round((sma50 / sma50_prev - 1) * 100, 2)
    return m


def condition_holds(check: ConditionCheck, metrics: dict[str, float]) -> bool | None:
    """True/False if evaluable; None if the metric isn't available (e.g. not
    enough history) — callers treat None as 'does not hold'."""
    if check.metric not in ALLOWED_METRICS or check.op not in ALLOWED_OPS:
        return None
    actual = metrics.get(check.metric)
    if actual is None:
        return None
    return {"<": actual < check.value, "<=": actual <= check.value,
            ">": actual > check.value, ">=": actual >= check.value}[check.op]


async def fetch_symbol_metrics(broker, symbol: str) -> dict[str, Any]:
    quote = await with_retry(broker.get_quote, symbol)
    hist = await with_retry(broker.get_historicals, symbol,
                            interval="day", span="3month")
    closes = [h["close"] for h in hist]
    metrics = compute_metrics(closes, quote["price"], quote.get("previous_close"))
    return {"symbol": symbol.upper(), "metrics": metrics,
            "quote_time": quote.get("updated_at")}


# --------------------------------------------------------------------- scan --

SCAN_SYSTEM_SUFFIX = """
MODE: RUNNING. You receive the user's strategy rules and a table of metrics this
app just computed from live broker data. Decide which of the USER'S OWN rules
actually fire on those exact numbers.

- Only metrics from this list may appear in condition_check: {metrics}
- Only operators: >=, <=, >, <
- matched_condition must quote the user's rule AND the exact numbers, e.g.
  "Your rule: buy when 3%+ below the 5-day high. Now: SPY $512.40 is 3.5% below
  its 5-day high of $531.10."
- A SELL may only reference a symbol listed under CURRENT POSITIONS.
- If nothing fires, return an empty proposals list with a one-line no_match_note.
- Options are out of scope; never propose anything but stock/ETF market orders.
"""


async def run_scan(db, llm, broker, settings, tenant, strategy_row,
                   llm_cap: int, max_position_pct: float) -> dict:
    """Returns {"proposals": [db-shaped dicts], "note": str, "skipped": [str]}.
    Raises LLMBudgetExceeded / NeedsAuth / BrokerError / SchemaValidationError —
    the Telegram layer turns those into honest user messages."""
    await require_llm_budget(db, llm_cap)

    rules = json.loads(strategy_row["rules"]) if isinstance(
        strategy_row["rules"], str) else strategy_row["rules"]
    watchlist = [s.upper() for s in rules.get("watchlist", [])][:15]
    if not watchlist:
        return {"proposals": [], "note": "Your strategy has no watchlist yet — "
                "run /interview first.", "skipped": []}

    snapshots: dict[str, dict] = {}
    skipped: list[str] = []
    for sym in watchlist:            # sequential: kind to the free-tier broker API
        try:
            snapshots[sym] = await fetch_symbol_metrics(broker, sym)
        except Exception as exc:     # noqa: BLE001 - reported, not swallowed
            if type(exc).__name__ == "NeedsAuth":
                raise
            skipped.append(f"{sym}: {exc}")

    if not snapshots:
        return {"proposals": [], "note": "No live data came back for any "
                "watchlist symbol.", "skipped": skipped}

    portfolio = await with_retry(broker.get_portfolio)
    positions = await with_retry(broker.get_positions)
    equity = _extract_equity(portfolio)
    held = _positions_by_symbol(positions)

    payload = {
        "STRATEGY_RULES": rules,
        "LIVE_METRICS": {s: v["metrics"] for s, v in snapshots.items()},
        "CURRENT_POSITIONS": {s: q for s, q in held.items()},
        "NOTE": "computed from live broker data at "
                + datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    system = (strategy_row.get("persona") or "") + SCAN_SYSTEM_SUFFIX.format(
        metrics=", ".join(ALLOWED_METRICS))
    await spend_llm_call(db)
    result: ScanResult = await complete_json(
        llm, system, [{"role": "user", "content": json.dumps(payload)}],
        ScanResult, max_tokens=3000)

    accepted: list[dict] = []
    for draft in result.proposals[:5]:
        sym = draft.symbol.upper()
        snap = snapshots.get(sym)
        reject = _vet_draft(draft, sym, snap, watchlist, held)
        if reject:
            skipped.append(f"{sym}: proposal discarded — {reject}")
            continue
        metrics = snap["metrics"]
        qty = _size_position(draft, equity, metrics["price"],
                             max_position_pct, held.get(sym, 0))
        if qty < 1:
            skipped.append(f"{sym}: position size under 1 whole share at "
                           f"${metrics['price']:.2f} — skipped")
            continue
        accepted.append({
            "action": draft.action, "symbol": sym, "qty": qty,
            "reason": draft.reason, "confidence": draft.confidence,
            "matched_condition": draft.matched_condition,
            "condition_check": draft.condition_check.model_dump(),
            "raw_values": metrics | {"quote_time": snap.get("quote_time")},
            "price_at_proposal": metrics["price"],
        })

    note = result.no_match_note if not accepted else None
    return {"proposals": accepted, "note": note, "skipped": skipped}


def _vet_draft(draft: ProposalDraft, sym: str, snap: dict | None,
               watchlist: list[str], held: dict[str, int]) -> str | None:
    """Numeric gate on every LLM claim. Returns a rejection reason or None."""
    if draft.action not in ("BUY", "SELL"):
        return f"invalid action {draft.action!r}"
    if sym not in watchlist:
        return "symbol not on your watchlist"
    if snap is None:
        return "no live data this scan"
    if draft.action == "SELL" and held.get(sym, 0) < 1:
        return "no position held to sell"
    holds = condition_holds(draft.condition_check, snap["metrics"])
    if holds is not True:
        cc = draft.condition_check
        actual = snap["metrics"].get(cc.metric)
        return (f"claimed condition {cc.metric} {cc.op} {cc.value} does not hold "
                f"on computed data (actual: {actual})")
    return None


def _size_position(draft: ProposalDraft, equity: float | None, price: float,
                   max_position_pct: float, held_qty: int) -> int:
    if draft.action == "SELL":
        return min(draft.qty, held_qty)
    if not equity or price <= 0:
        return 0
    cap_qty = math.floor((equity * max_position_pct / 100.0) / price)
    return max(0, min(draft.qty, cap_qty))


# -------------------------------------------------- pre-order re-verification --

async def verify_condition_fresh(broker, proposal_row) -> tuple[bool, dict]:
    """Re-fetch live data NOW and re-evaluate the proposal's original
    metric/op/threshold. Same request as the order, no delay, no LLM involved."""
    check = ConditionCheck(**_as_dict(proposal_row["condition_check"]))
    snap = await fetch_symbol_metrics(broker, proposal_row["symbol"])
    holds = condition_holds(check, snap["metrics"])
    fresh = snap["metrics"] | {"quote_time": snap.get("quote_time"),
                               "checked": f"{check.metric} {check.op} {check.value}"}
    return holds is True, fresh


# ------------------------------------------------------------------ helpers --

def _as_dict(v: Any) -> dict:
    return json.loads(v) if isinstance(v, str) else dict(v)


def _extract_equity(portfolio: dict) -> float | None:
    for k in ("equity", "portfolio_value", "market_value", "total_equity"):
        v = portfolio.get(k)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _positions_by_symbol(positions: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in positions:
        sym = (p.get("symbol") or p.get("ticker") or "").upper()
        try:
            q = int(float(p.get("quantity") or p.get("qty") or 0))
        except (TypeError, ValueError):
            q = 0
        if sym and q > 0:
            out[sym] = q
    return out
