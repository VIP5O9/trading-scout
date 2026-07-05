"""SETUP mode: the fixed 5-question strategy interview, run as a Telegram
conversation. Free text goes in; validated, machine-checkable rules come out;
NOTHING is saved until the user reads the plain-English read-back and replies YES.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from src.llm.fallback import complete_json
from src.modes.running import ALLOWED_METRICS, ALLOWED_OPS
from src.safety.ratelimit import require_llm_budget, spend_llm_call

QUESTIONS = {
    1: "1/5 — Which stocks or ETFs do you want to watch? (Names or tickers, "
       "up to 15. Example: \"Apple, SPY and the Nasdaq ETF\")",
    2: "2/5 — In your own words: what makes you want to BUY something on that list?",
    3: "3/5 — In your own words: what makes you want to SELL or take profit?",
    4: "4/5 — How much of your account are you comfortable putting into a single "
       "position? (A percentage like 5%, or a dollar amount plus your rough "
       "account size.)",
    5: "5/5 — How often do you plan to check in? (Just so I can describe your "
       "routine back to you — I never scan on my own; you always trigger scans "
       "yourself with /scan.)",
}

# Deterministic options screen for the buy/sell answers. Word-bounded so plain
# phrases like "put money in" don't trip it; the LLM's options_requested flag
# backstops anything this misses.
_OPTIONS_RX = re.compile(
    r"\b(options?|covered calls?|call options?|put options?|calls|puts|spreads?|"
    r"strike|premium|expirations?|leaps|straddles?|strangles?|condors?|0 ?dte)\b",
    re.IGNORECASE)

OPTIONS_REFUSAL = (
    "That sounds like an options strategy — this app supports stocks and ETFs "
    "only, no options of any kind. Please describe what would make you want to "
    "{verb} a stock or ETF instead (for example: \"when it drops a few percent "
    "from its recent high\").")

_TICKER_RX = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


class StrategyRule(BaseModel):
    side: str                        # buy | sell
    metric: str
    op: str
    value: float
    plain_english: str

    @field_validator("metric")
    @classmethod
    def _metric_allowed(cls, v: str) -> str:
        if v not in ALLOWED_METRICS:
            raise ValueError(f"metric must be one of {ALLOWED_METRICS}")
        return v

    @field_validator("op")
    @classmethod
    def _op_allowed(cls, v: str) -> str:
        if v not in ALLOWED_OPS:
            raise ValueError(f"op must be one of {ALLOWED_OPS}")
        return v


class StrategyRules(BaseModel):
    watchlist: list[str] = Field(max_length=15)
    buy_rules: list[StrategyRule] = Field(default_factory=list)
    sell_rules: list[StrategyRule] = Field(default_factory=list)
    max_position_pct: float = Field(ge=0.5, le=25.0)
    cadence_note: str = ""
    untranslatable: list[str] = Field(default_factory=list)
    options_requested: bool = False

    @field_validator("watchlist")
    @classmethod
    def _tickers(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().upper()
            if s and _TICKER_RX.match(s):
                out.append(s)
        if not out:
            raise ValueError("watchlist resolved to no valid tickers")
        return out


TRANSLATE_SUFFIX = """
MODE: SETUP translation. Turn the interview answers into StrategyRules JSON.
- Resolve company names to tickers (e.g. "Apple" -> AAPL, "the Nasdaq ETF" -> QQQ).
- Rules use ONLY these metrics: {metrics} and operators >=, <=, >, <.
- Convert dollar risk answers to a percent of the stated account size.
- Anything you cannot express in those metrics goes into `untranslatable`
  verbatim — NEVER force a translation or guess.
- If any answer describes options (covered calls, puts, spreads, premium),
  set options_requested=true and do not translate that part.
- plain_english on every rule: one short sentence a non-trader understands.
"""


async def begin(db, tg, tenant, chat_id: int) -> None:
    await db.set_session(chat_id, tenant["id"], "interview_1", {"answers": {}})
    await tg.send(chat_id,
                  "Let's set up your strategy — 5 quick questions, then I read "
                  "the whole thing back and you approve it before anything is "
                  "saved.\n\n" + QUESTIONS[1])


async def handle_message(db, llm, tg, settings, engine_prompt: str, tenant,
                         chat_id: int, state: str, state_data: dict,
                         text: str) -> None:
    answers: dict = state_data.get("answers", {})

    if state.startswith("interview_"):
        step = int(state.split("_")[1])
        if step in (2, 3) and _OPTIONS_RX.search(text):
            verb = "buy" if step == 2 else "sell"
            await tg.send(chat_id, OPTIONS_REFUSAL.format(verb=verb))
            return                                   # same question, re-ask
        answers[str(step)] = text.strip()
        if step < 5:
            await db.set_session(chat_id, tenant["id"], f"interview_{step + 1}",
                                 {"answers": answers})
            await tg.send(chat_id, QUESTIONS[step + 1])
            return
        await _translate_and_read_back(db, llm, tg, settings, engine_prompt,
                                       tenant, chat_id, answers, edits=None)
        return

    if state == "readback":
        if text.strip().upper() in ("YES", "Y", "YES.", "YES!"):
            await _save(db, tg, tenant, chat_id, state_data)
            return
        # Anything else is an edit instruction: re-translate with it applied.
        await _translate_and_read_back(db, llm, tg, settings, engine_prompt,
                                       tenant, chat_id,
                                       state_data.get("answers", {}),
                                       edits=text.strip())
        return


async def _translate_and_read_back(db, llm, tg, settings, engine_prompt, tenant,
                                   chat_id, answers: dict,
                                   edits: str | None) -> None:
    await require_llm_budget(db, settings.default_max_llm_calls_per_day)
    payload = {"INTERVIEW_ANSWERS": answers}
    if edits:
        payload["USER_EDIT_REQUEST"] = edits
        payload["PREVIOUS_UNDERSTANDING"] = "apply the edit to the answers above"
    system = engine_prompt + TRANSLATE_SUFFIX.format(
        metrics=", ".join(ALLOWED_METRICS))
    await spend_llm_call(db)
    try:
        rules: StrategyRules = await complete_json(
            llm, system, [{"role": "user", "content": json.dumps(payload)}],
            StrategyRules, max_tokens=2500)
    except Exception as exc:  # SchemaValidationError, LLMError — surfaced honestly
        await tg.send(chat_id,
                      "I couldn't turn that into checkable rules just now "
                      f"(technical reason: {type(exc).__name__}: {exc}). "
                      "Nothing was saved. Try /interview again in a minute.")
        await db.set_session(chat_id, tenant["id"], "ready",
                             {"answers": answers})
        return

    if rules.options_requested:
        await db.set_session(chat_id, tenant["id"], "interview_2",
                             {"answers": {k: v for k, v in answers.items()
                                          if k in ("1", "4", "5")}})
        await tg.send(chat_id, OPTIONS_REFUSAL.format(verb="buy") +
                      "\n\n" + QUESTIONS[2])
        return

    readback = _plain_english_readback(rules)
    await db.set_session(chat_id, tenant["id"], "readback",
                         {"answers": answers,
                          "rules": rules.model_dump()})
    await tg.send(chat_id, readback +
                  "\n\nDid I get that right? Reply <b>YES</b> to save, or tell "
                  "me what to change.")


def _plain_english_readback(r: StrategyRules) -> str:
    lines = ["📝 <b>Here's what I understood:</b>", "",
             "Watching: " + ", ".join(r.watchlist)]
    for rule in r.buy_rules:
        lines.append(f"• BUY when {rule.plain_english} "
                     f"({rule.metric} {rule.op} {rule.value})")
    for rule in r.sell_rules:
        lines.append(f"• SELL when {rule.plain_english} "
                     f"({rule.metric} {rule.op} {rule.value})")
    lines.append(f"• Max {r.max_position_pct:g}% of your account in any one "
                 "position (whole shares only)")
    if r.cadence_note:
        lines.append(f"• Your routine: {r.cadence_note} (reminder: scans only "
                     "run when you send /scan)")
    if r.untranslatable:
        lines.append("")
        lines.append("⚠️ I could NOT turn these into checkable rules, so they "
                     "are NOT part of your strategy:")
        for u in r.untranslatable:
            lines.append(f"  – “{u}”")
        lines.append("You can rephrase them now, or reply YES to save without them.")
    return "\n".join(lines)


async def _save(db, tg, tenant, chat_id: int, state_data: dict) -> None:
    rules = state_data.get("rules")
    if not rules:
        await tg.send(chat_id, "Nothing pending to save — run /interview.")
        return
    strategy_md = _strategy_md(rules, state_data.get("answers", {}))
    config_yaml = yaml.safe_dump({
        "tenant": tenant["name"],
        "risk": {"max_position_pct": rules["max_position_pct"],
                 "max_llm_calls_per_day": 40},
        "broker": {"type": "robinhood", "oauth": "browser"},
    }, sort_keys=False)
    version = await db.save_strategy(tenant["id"], strategy_md, rules, config_yaml)
    _mirror_to_disk(tenant["name"], config_yaml, strategy_md)
    await db.set_session(chat_id, tenant["id"], "ready", {})
    await tg.send(chat_id,
                  f"✅ Saved as strategy v{version}. Send /scan any time to "
                  "check your rules against live prices. /strategy shows what's "
                  "saved; /interview starts over.")


def _strategy_md(rules: dict, answers: dict) -> str:
    md = ["# Strategy (from the SETUP interview)", "",
          "## Watchlist", ", ".join(rules["watchlist"]), "", "## Buy rules"]
    for r in rules.get("buy_rules", []):
        md.append(f"- {r['plain_english']}  "
                  f"`{r['metric']} {r['op']} {r['value']}`")
    md += ["", "## Sell rules"]
    for r in rules.get("sell_rules", []):
        md.append(f"- {r['plain_english']}  "
                  f"`{r['metric']} {r['op']} {r['value']}`")
    md += ["", f"## Position size\nMax {rules['max_position_pct']}% per position.",
           "", "## Original answers (verbatim)"]
    for k in sorted(answers):
        md.append(f"{k}. {answers[k]}")
    return "\n".join(md)


def _mirror_to_disk(name: str, config_yaml: str, strategy_md: str) -> None:
    """Best-effort mirror to tenants/{name}/ for transparency. Render's disk is
    ephemeral, so the database copy written above is the source of truth."""
    try:
        d = Path("tenants") / re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(config_yaml, encoding="utf-8")
        (d / "strategy.md").write_text(strategy_md, encoding="utf-8")
    except OSError:
        pass
