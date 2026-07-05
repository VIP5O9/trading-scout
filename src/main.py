"""trade-scout entrypoint: one FastAPI service.

- POST /telegram/webhook — the entire bot: interview, scans, proposals, and the
  ONLY code path in this repo that can lead to an order (two-tap confirm).
- GET  /oauth/callback   — Robinhood browser-login return leg.
- GET  /, /portfolio, /sectors — read-only dashboard, gated to the bot owner via
  one-time /weblink magic links. No mutating route exists on the web side.

Execution model (permanent shape, not a setting):
  scan -> proposal card [View Detail] -> detail view [Confirm Buy/Sell]
  -> atomic claim (shown->confirming) -> RE-FETCH live data and re-evaluate the
  ORIGINAL numeric rule -> only then place_order. Broker errors are shown
  verbatim and STOP the flow — no retry, ever.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.broker.robinhood import BrokerError, NeedsAuth, RobinhoodBroker
from src.config import load_settings
from src.data.finnhub import FinnhubCapError, FinnhubClient, FinnhubError
from src.db import Database
from src.intel.brief import market_brief
from src.intel.sector import sector_momentum
from src.llm.fallback import SchemaValidationError
from src.llm.runtime import LLMClient, LLMError
from src.modes import setup as setup_mode
from src.modes.running import run_scan, verify_condition_fresh
from src.notif.telegram import (TelegramClient, confirm_keyboard,
                                detail_keyboard, esc, format_proposal_card,
                                format_proposal_detail)
from src.safety.ratelimit import LLMBudgetExceeded

ROOT = Path(__file__).resolve().parent.parent


class App:
    """Shared singletons (built at startup)."""
    settings = None
    db: Database = None
    tg: TelegramClient = None
    broker: RobinhoodBroker = None
    llm: LLMClient = None
    finnhub: FinnhubClient = None
    engine_prompt: str = ""
    web_sessions: set[str] = set()   # in-memory; a restart just means /weblink again


A = App()
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))


def _load_engine_prompt() -> str:
    parts = []
    for name in ("persona.md", "core_rules.md", "translator_rules.md"):
        p = ROOT / "engine" / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    A.settings = load_settings()
    A.db = Database()
    await A.db.init(A.settings.database_url)
    A.tg = TelegramClient(A.settings.telegram_bot_token)
    A.broker = RobinhoodBroker(A.settings.robinhood_mcp_url,
                               A.settings.base_url + "/oauth/callback")
    A.llm = LLMClient(A.settings.llm_provider, A.settings.llm_api_key)
    A.finnhub = FinnhubClient(A.settings.finnhub_api_key, A.db)
    A.engine_prompt = _load_engine_prompt()
    await A.tg.set_webhook(A.settings.base_url + "/telegram/webhook")
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "web" / "static")),
          name="static")


# ============================================================== tenant utils ==

async def _tenant_cfg(tenant) -> dict:
    row = await A.db.pool.fetchrow(
        "SELECT yaml FROM config WHERE tenant_id=$1", tenant["id"])
    cfg = yaml.safe_load(row["yaml"]) if row else {}
    risk = (cfg or {}).get("risk") or {}
    return {
        "max_position_pct": float(risk.get(
            "max_position_pct", A.settings.default_max_position_pct)),
        "llm_cap": int(risk.get(
            "max_llm_calls_per_day", A.settings.default_max_llm_calls_per_day)),
    }


async def _watchlist(tenant) -> list[str]:
    strat = await A.db.get_active_strategy(tenant["id"])
    if not strat:
        return []
    import json as _json
    rules = strat["rules"]
    if isinstance(rules, str):
        rules = _json.loads(rules)
    return [s.upper() for s in rules.get("watchlist", [])]


# ============================================================ telegram webhook ==

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") \
            != A.tg.webhook_secret:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    update_id = update.get("update_id")
    # Telegram retries webhooks that time out (Render cold starts) — process
    # each update exactly once so a retry can never double-handle a confirm.
    if update_id is None or not await A.db.first_time_update(int(update_id)):
        return {"ok": True}
    try:
        if "message" in update:
            await _on_message(update["message"])
        elif "callback_query" in update:
            await _on_callback(update["callback_query"])
    except Exception as exc:  # noqa: BLE001 — last-resort honest report
        chat_id = _chat_of(update)
        if chat_id:
            await A.tg.send(chat_id, "⚠️ Something went wrong handling that:\n"
                            f"<code>{esc(str(exc)[:400])}</code>")
    return {"ok": True}


def _chat_of(update: dict) -> int | None:
    msg = update.get("message") or (update.get("callback_query") or {}).get("message")
    return (msg or {}).get("chat", {}).get("id")


async def _gate(chat_id: int, from_user: dict, is_start: bool) -> "record | None":
    """THE TENANT LOCK. First /start claims ownership; every other sender is
    rejected before any processing — no interview, no proposals, no OAuth."""
    tenant = await A.db.get_tenant()
    if tenant is None:
        if not is_start:
            await A.tg.send(chat_id, "Send /start to begin.")
            return None
        name = (from_user.get("username") or f"user{chat_id}")[:40]
        return await A.db.claim_owner(name, chat_id)
    if tenant["owner_chat_id"] != chat_id:
        await A.tg.send(chat_id, "This bot is private.")
        return None
    return tenant


# ------------------------------------------------------------------ messages --

async def _on_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    cmd = text.split()[0].lower() if text.startswith("/") else None
    tenant = await _gate(chat_id, msg.get("from") or {}, cmd == "/start")
    if tenant is None:
        return

    if cmd == "/start":
        await _cmd_start(tenant, chat_id)
    elif cmd == "/interview":
        await setup_mode.begin(A.db, A.tg, tenant, chat_id)
    elif cmd == "/connect":
        await _cmd_connect(chat_id)
    elif cmd == "/scan":
        await _cmd_scan(tenant, chat_id)
    elif cmd == "/portfolio":
        await _cmd_portfolio(chat_id)
    elif cmd == "/sectors":
        await _cmd_sectors(chat_id)
    elif cmd == "/brief":
        await _cmd_brief(tenant, chat_id)
    elif cmd == "/news":
        await _cmd_news(tenant, chat_id, text)
    elif cmd == "/strategy":
        await _cmd_strategy(tenant, chat_id)
    elif cmd == "/weblink":
        await _cmd_weblink(tenant, chat_id)
    elif cmd in ("/help", "/h"):
        await _cmd_help(chat_id)
    elif cmd:
        await A.tg.send(chat_id, "Unknown command — /help lists everything.")
    else:
        # Free text: only meaningful inside the interview/read-back flow.
        session = await A.db.get_session(chat_id)
        state = session["state"] if session else "new"
        if state.startswith("interview_") or state == "readback":
            import json as _json
            state_data = session["state_data"]
            if isinstance(state_data, str):
                state_data = _json.loads(state_data)
            await setup_mode.handle_message(
                A.db, A.llm, A.tg, A.settings, A.engine_prompt, tenant,
                chat_id, state, state_data, text)
        else:
            await A.tg.send(chat_id, "I only act on commands — /help lists "
                            "them. (I never trade from chat messages.)")


async def _cmd_start(tenant, chat_id: int) -> None:
    await A.tg.send(chat_id, (
        "👋 You're set up as this bot's <b>only</b> authorized user — no other "
        "Telegram account can ever talk to it.\n\n"
        "<b>Before anything else, one thing on Robinhood's side:</b> this app "
        "talks to Robinhood's agentic-trading service, and that has to be "
        "enabled on YOUR Robinhood account first — you do that in Robinhood "
        "itself, not here. Once it's enabled, /connect gives you a login link "
        "(you log in on Robinhood's own page; I never see your password).\n\n"
        "Nothing is ever bought or sold without you tapping Confirm yourself, "
        "twice, right in this chat.\n\n"
        "Let's capture your strategy first — you can /connect whenever."))
    await setup_mode.begin(A.db, A.tg, tenant, chat_id)


async def _cmd_connect(chat_id: int) -> None:
    try:
        url = await A.broker.get_auth_url()
    except BrokerError as exc:
        await A.tg.send(chat_id, f"⚠️ {esc(str(exc))}")
        return
    if url is None:
        await A.tg.send(chat_id, "✅ Robinhood is connected.")
    else:
        await A.tg.send(chat_id,
                        "Open this link, log in on Robinhood's own page, and "
                        "approve access. Then come back here:\n" + esc(url) +
                        "\n\n(If the login page errors, agentic trading is "
                        "probably not enabled on your Robinhood account yet — "
                        "see BROKER_INTEGRATION.md.)")


async def _cmd_scan(tenant, chat_id: int) -> None:
    strat = await A.db.get_active_strategy(tenant["id"])
    if not strat:
        await A.tg.send(chat_id, "No strategy saved yet — run /interview first.")
        return
    cfg = await _tenant_cfg(tenant)
    await A.tg.send(chat_id, "🔍 Scanning your watchlist against live data…")
    try:
        strat_d = dict(strat)
        strat_d["persona"] = A.engine_prompt
        out = await run_scan(A.db, A.llm, A.broker, A.settings, tenant, strat_d,
                             cfg["llm_cap"], cfg["max_position_pct"])
    except NeedsAuth as e:
        await A.tg.send(chat_id, "Robinhood login needed first:\n" +
                        esc(e.auth_url))
        return
    except LLMBudgetExceeded as e:
        await A.tg.send(chat_id, f"⏸ {esc(str(e))}")
        return
    except (BrokerError, LLMError, SchemaValidationError) as e:
        await A.tg.send(chat_id,
                        f"⚠️ Scan stopped ({type(e).__name__}):\n"
                        f"<code>{esc(str(e)[:400])}</code>\nNothing was "
                        "proposed on partial/unvalidated data.")
        return
    await A.db.set_meta(tenant["id"], "last_scan_at",
                        __import__("datetime").datetime.utcnow()
                        .strftime("%Y-%m-%d %H:%M UTC"))
    for p in out["proposals"]:
        pid = await A.db.insert_proposal(tenant["id"], p)
        await A.tg.send(chat_id, format_proposal_card(p), detail_keyboard(pid))
    if not out["proposals"]:
        await A.tg.send(chat_id, "✅ Scan done — no rule fired. "
                        + esc(out.get("note") or ""))
    if out["skipped"]:
        await A.tg.send(chat_id, "ℹ️ Notes:\n" +
                        "\n".join("• " + esc(s) for s in out["skipped"][:8]))


async def _cmd_portfolio(chat_id: int) -> None:
    try:
        pf = await A.broker.get_portfolio()
        positions = await A.broker.get_positions()
    except NeedsAuth as e:
        await A.tg.send(chat_id, "Robinhood login needed:\n" + esc(e.auth_url))
        return
    except BrokerError as e:
        await A.tg.send(chat_id, f"⚠️ {esc(str(e))}")
        return
    lines = ["💼 <b>Portfolio</b> (read-only)"]
    eq = pf.get("equity") or pf.get("portfolio_value")
    if eq is not None:
        lines.append(f"Account value: ${float(eq):,.2f}")
    bp = pf.get("buying_power") or pf.get("cash")
    if bp is not None:
        lines.append(f"Buying power: ${float(bp):,.2f}")
    if not positions:
        lines.append("No positions.")
    for p in positions[:20]:
        sym = p.get("symbol") or p.get("ticker") or "?"
        qty = p.get("quantity") or p.get("qty") or "?"
        avg = p.get("average_buy_price") or p.get("avg_cost")
        row = f"• {esc(sym)}: {esc(qty)} sh"
        if avg is not None:
            row += f" @ ${float(avg):,.2f}"
        lines.append(row)
    await A.tg.send(chat_id, "\n".join(lines))


async def _cmd_sectors(chat_id: int) -> None:
    try:
        rows = await sector_momentum(A.broker)
    except NeedsAuth as e:
        await A.tg.send(chat_id, "Robinhood login needed:\n" + esc(e.auth_url))
        return
    except BrokerError as e:
        await A.tg.send(chat_id, f"⚠️ {esc(str(e))}")
        return
    lines = ["📊 <b>Sector momentum — 20-day return</b>"]
    for r in rows:
        if "error" in r:
            lines.append(f"• {esc(r['symbol'])}: {esc(r['error'])}")
        else:
            arrow = "🟢" if r["return_20d_pct"] >= 0 else "🔴"
            lines.append(f"{arrow} {r['rank']}. {esc(r['symbol'])}  "
                         f"{r['return_20d_pct']:+.2f}%")
    lines.append("<i>Information, not a recommendation.</i>")
    await A.tg.send(chat_id, "\n".join(lines))


async def _cmd_brief(tenant, chat_id: int) -> None:
    wl = await _watchlist(tenant)
    if not wl:
        await A.tg.send(chat_id, "No watchlist yet — run /interview first.")
        return
    cfg = await _tenant_cfg(tenant)
    try:
        text = await market_brief(A.db, A.llm, A.broker, A.engine_prompt,
                                  wl, cfg["llm_cap"])
    except NeedsAuth as e:
        await A.tg.send(chat_id, "Robinhood login needed:\n" + esc(e.auth_url))
        return
    except LLMBudgetExceeded as e:
        await A.tg.send(chat_id, f"⏸ {esc(str(e))}")
        return
    except (BrokerError, LLMError) as e:
        await A.tg.send(chat_id, f"⚠️ Brief unavailable: {esc(str(e)[:300])}")
        return
    await A.tg.send(chat_id, text)


async def _cmd_news(tenant, chat_id: int, text: str) -> None:
    parts = text.split()
    wl = await _watchlist(tenant)
    symbols = [parts[1].upper()] if len(parts) > 1 else wl[:5]
    if not symbols:
        await A.tg.send(chat_id, "No watchlist yet — run /interview, or use "
                        "/news SYMBOL.")
        return
    lines = ["📰 <b>News</b> (Finnhub, cached up to 1h)"]
    for sym in symbols:
        try:
            items = await A.finnhub.company_news(sym)
        except FinnhubCapError as e:
            lines.append(f"⏸ {esc(str(e))}")
            break
        except FinnhubError as e:
            lines.append(f"• {esc(sym)}: news unavailable ({esc(str(e)[:120])})")
            continue
        lines.append(f"<b>{esc(sym)}</b>")
        if not items:
            lines.append("  (no recent headlines)")
        for it in items[:3]:
            lines.append(f"  • <a href=\"{esc(it.get('url') or '')}\">"
                         f"{esc(it.get('headline') or '')}</a>")
    await A.tg.send(chat_id, "\n".join(lines))


async def _cmd_strategy(tenant, chat_id: int) -> None:
    strat = await A.db.get_active_strategy(tenant["id"])
    if not strat:
        await A.tg.send(chat_id, "No strategy saved yet — run /interview.")
        return
    await A.tg.send(chat_id,
                    f"📜 Strategy v{strat['version']} "
                    f"(saved {strat['created_at']:%Y-%m-%d}):\n\n"
                    f"<code>{esc(strat['strategy_md'][:3000])}</code>")


async def _cmd_weblink(tenant, chat_id: int) -> None:
    token = await A.db.create_web_token(tenant["id"])
    await A.tg.send(chat_id,
                    "Your one-time dashboard link (valid 10 minutes, works "
                    "once):\n" + esc(f"{A.settings.base_url}/auth/{token}"))


async def _cmd_help(chat_id: int) -> None:
    await A.tg.send(chat_id, (
        "<b>Commands</b>\n"
        "/scan — check your rules against live prices now\n"
        "/portfolio — positions & account value (read-only)\n"
        "/sectors — 20-day sector momentum ranking\n"
        "/brief — LLM market summary (labeled interpretation)\n"
        "/news [SYMBOL] — headlines for your watchlist\n"
        "/strategy — show the saved strategy\n"
        "/interview — redo the 5-question setup\n"
        "/connect — Robinhood login link\n"
        "/weblink — one-time link to the read-only dashboard\n\n"
        "Orders happen ONLY via a proposal's View Detail → Confirm buttons — "
        "two taps, always, with a live re-check in between."))


# ------------------------------------------------------------------ callbacks --

async def _on_callback(cb: dict) -> None:
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    data = cb.get("data") or ""
    cb_id = cb.get("id", "")
    if not chat_id:
        return
    tenant = await _gate(chat_id, cb.get("from") or {}, False)
    if tenant is None:
        await A.tg.answer_callback(cb_id)
        return
    action, _, pid = data.partition(":")
    if action == "detail":
        await _show_detail(chat_id, pid)
    elif action == "confirm":
        await _confirm_and_place(tenant, chat_id, pid)
    elif action == "reject":
        await A.db.finalize_proposal(pid, "rejected", note="dismissed by user")
        await A.tg.send(chat_id, "Dismissed. Logged, nothing placed.")
    await A.tg.answer_callback(cb_id)


async def _show_detail(chat_id: int, pid: str) -> None:
    """TAP 1 of 2: show raw values + the separate Confirm button."""
    row = await A.db.get_proposal(pid)
    if row is None:
        await A.tg.send(chat_id, "That proposal no longer exists.")
        return
    if row["status"] != "shown":
        await A.tg.send(chat_id, f"This proposal is already "
                        f"<b>{esc(row['status'])}</b> — nothing to do.")
        return
    import json as _json
    raw = row["raw_values"]
    if isinstance(raw, str):
        raw = _json.loads(raw)
    p = dict(row)
    await A.tg.send(chat_id, format_proposal_detail(p, raw),
                    confirm_keyboard(pid, row["action"]))


async def _confirm_and_place(tenant, chat_id: int, pid: str) -> None:
    """TAP 2 of 2 — the ONLY path to place_order in this entire codebase.

    Sequence: atomic claim -> fresh re-verification of the ORIGINAL numeric
    rule -> place -> log. Any failure stops with the exact reason. No retry."""
    row = await A.db.claim_for_confirm(pid)
    if row is None:
        cur = await A.db.get_proposal(pid)
        status = cur["status"] if cur else "gone"
        await A.tg.send(chat_id, f"Not placed — this proposal is already "
                        f"<b>{esc(status)}</b>.")
        return

    # Re-fetch live data NOW and re-check the original rule (same request).
    try:
        holds, fresh = await verify_condition_fresh(A.broker, row)
    except NeedsAuth as e:
        await A.db.finalize_proposal(pid, "error",
                                     note="auth expired at confirm")
        await A.tg.send(chat_id, "Not placed — Robinhood login expired. "
                        "Log in and scan again:\n" + esc(e.auth_url))
        return
    except BrokerError as e:
        await A.db.finalize_proposal(pid, "error",
                                     note=f"reverify failed: {e}")
        await A.tg.send(chat_id, "Not placed — couldn't re-verify with live "
                        f"data:\n<code>{esc(str(e)[:300])}</code>")
        return

    if not holds:
        await A.db.finalize_proposal(pid, "stale",
                                     execution_result={"fresh_check": fresh})
        cc = row["condition_check"]
        await A.tg.send(chat_id, (
            "🛑 <b>Not placed.</b> The rule that fired no longer holds on "
            "live data:\n"
            f"Rule: <code>{esc(str(cc))}</code>\n"
            f"Fresh values: <code>{esc(str(fresh))}</code>\n"
            "Run /scan again if you still want to look."))
        return

    side = "buy" if row["action"] == "BUY" else "sell"
    try:
        result = await A.broker.place_order(row["symbol"], side, row["qty"])
    except (BrokerError, NeedsAuth) as e:
        # Exact error, verbatim, then STOP. No retry on order placement.
        msg = e.auth_url if isinstance(e, NeedsAuth) else str(e)
        await A.db.finalize_proposal(pid, "error",
                                     execution_result={"error": str(e)[:800]})
        await A.tg.send(chat_id, "❌ <b>Order NOT placed.</b> Robinhood said:\n"
                        f"<code>{esc(msg[:400])}</code>\n"
                        "Nothing was retried. Check the app if unsure, then "
                        "/scan again when ready.")
        return

    await A.db.finalize_proposal(pid, "confirmed", execution_result=result)
    await A.tg.send(chat_id, (
        f"✅ <b>Order placed:</b> {esc(row['action'])} {row['qty']} "
        f"{esc(row['symbol'])} (market, day).\n"
        f"Fresh check passed: <code>{esc(str(fresh.get('checked')))}</code>\n"
        "Robinhood's reply is logged. /portfolio to see it land."))


# ================================================================== oauth ==

@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse("<h3>Robinhood login was cancelled or failed."
                            "</h3><p>Return to Telegram and send /connect to "
                            "try again.</p>", status_code=400)
    try:
        await A.broker.handle_oauth_callback(code, state)
    except BrokerError as e:
        return HTMLResponse(f"<h3>Login failed</h3><p>{e}</p>"
                            "<p>Return to Telegram and send /connect.</p>",
                            status_code=400)
    tenant = await A.db.get_tenant()
    if tenant and tenant["owner_chat_id"]:
        await A.tg.send(tenant["owner_chat_id"],
                        "✅ Robinhood connected. You can close the browser tab. "
                        "(You'll be asked to log in again after the app has "
                        "been asleep — that's by design; nothing is stored.)")
    return HTMLResponse("<h3>✅ Connected.</h3><p>You can close this tab and "
                        "go back to Telegram.</p>")


# ================================================================== web UI ==

def _web_ok(request: Request) -> bool:
    sid = request.cookies.get("ts_session")
    return bool(sid) and sid in A.web_sessions


@app.get("/auth/{token}")
async def web_auth(token: str):
    tenant_id = await A.db.consume_web_token(token)
    if tenant_id is None:
        return HTMLResponse("<h3>Link expired or already used.</h3>"
                            "<p>Send /weblink to your bot for a fresh one.</p>",
                            status_code=403)
    sid = secrets.token_urlsafe(32)
    A.web_sessions.add(sid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("ts_session", sid, httponly=True, secure=True,
                    samesite="lax", max_age=7 * 24 * 3600)
    return resp


def _denied(request: Request):
    return templates.TemplateResponse(
        request, "denied.html", {"active": "status"}, status_code=403)


@app.get("/", response_class=HTMLResponse)
async def web_index(request: Request):
    if not _web_ok(request):
        return _denied(request)
    tenant = await A.db.get_tenant()
    if tenant is None:
        return HTMLResponse("<p>No owner yet — send /start to the bot.</p>")
    cfg = await _tenant_cfg(tenant)
    strat = await A.db.get_active_strategy(tenant["id"])
    used = await A.db.count_api_calls_today("llm")
    return templates.TemplateResponse(request, "index.html", {
        "active": "status",
        "tenant_name": tenant["name"],
        "strategy_summary": (strat["strategy_md"] if strat
                             else "No strategy saved yet — run /interview."),
        "proposals_today": await A.db.proposals_today(tenant["id"]),
        "llm_used": used, "llm_cap": cfg["llm_cap"],
        "last_scan": await A.db.get_meta(tenant["id"], "last_scan_at") or "never",
    })


@app.get("/portfolio", response_class=HTMLResponse)
async def web_portfolio(request: Request):
    if not _web_ok(request):
        return _denied(request)
    try:
        pf = await A.broker.get_portfolio()
        raw = await A.broker.get_positions()
    except (NeedsAuth, BrokerError):
        pf, raw = {}, []
    positions = []
    for p in raw:
        try:
            qty = float(p.get("quantity") or 0)
            avg = float(p.get("average_buy_price") or 0)
            last = float(p.get("last_trade_price") or p.get("price") or 0)
            pnl = (last - avg) * qty
            positions.append({
                "symbol": p.get("symbol") or "?", "qty": f"{qty:g}",
                "avg_cost": f"${avg:,.2f}", "last_price": f"${last:,.2f}",
                "pnl_usd": f"${pnl:+,.2f}",
                "pnl_pct": (f"{(last / avg - 1) * 100:+.2f}%" if avg else "—"),
                "pnl_positive": pnl >= 0,
            })
        except (TypeError, ValueError):
            continue
    eq = pf.get("equity") or pf.get("portfolio_value")
    bp = pf.get("buying_power") or pf.get("cash")
    return templates.TemplateResponse(request, "portfolio.html", {
        "active": "portfolio",
        "equity": f"${float(eq):,.2f}" if eq is not None else "connect broker",
        "buying_power": f"${float(bp):,.2f}" if bp is not None else "—",
        "positions": positions,
    })


@app.get("/sectors", response_class=HTMLResponse)
async def web_sectors(request: Request):
    if not _web_ok(request):
        return _denied(request)
    try:
        ranked = await sector_momentum(A.broker)
    except (NeedsAuth, BrokerError):
        ranked = []
    rows = []
    vals = [abs(r["return_20d_pct"]) for r in ranked if "error" not in r] or [1]
    scale = max(vals)
    for r in ranked:
        if "error" in r:
            continue
        rows.append({
            "rank": r["rank"], "symbol": r["symbol"],
            "return_20d_pct": r["return_20d_pct"],
            "bar_pct": int(abs(r["return_20d_pct"]) / scale * 100),
            "last_close": f"${r['last_close']:,.2f}",
        })
    from datetime import datetime, timezone
    return templates.TemplateResponse(request, "sectors.html", {
        "active": "sectors", "rows": rows,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


@app.get("/healthz")
async def healthz():
    return {"ok": True}
