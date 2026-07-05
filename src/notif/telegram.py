"""Telegram Bot API client. Telegram IS the primary interface: the interview,
proposal cards, and the two-tap confirm all happen here as one private chat
between the owner and their own bot."""
from __future__ import annotations

import hashlib
import html
from typing import Any

import httpx


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, bot_token: str):
        self._token = bot_token
        self._http = httpx.AsyncClient(timeout=30.0)
        # Shared secret Telegram echoes back on every webhook call, so the
        # webhook route can reject posts that aren't from Telegram. Derived,
        # not stored; the bot token itself never appears in any URL we log.
        self.webhook_secret = hashlib.sha256(
            b"trade-scout-webhook:" + bot_token.encode()).hexdigest()[:40]

    async def _api(self, method: str, payload: dict) -> Any:
        r = await self._http.post(
            f"https://api.telegram.org/bot{self._token}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description')}")
        return data.get("result")

    async def set_webhook(self, url: str) -> None:
        await self._api("setWebhook", {
            "url": url,
            "secret_token": self.webhook_secret,
            "allowed_updates": ["message", "callback_query"],
        })

    async def send(self, chat_id: int, text: str,
                   keyboard: list[list[dict]] | None = None) -> Any:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return await self._api("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:190]
        try:
            await self._api("answerCallbackQuery", payload)
        except TelegramError:
            pass  # stale callback ids (webhook retries) are fine to ignore


def esc(s: Any) -> str:
    """HTML-escape anything user- or market-derived before it hits parse_mode=HTML."""
    return html.escape(str(s), quote=False)


# ---------------------------------------------------------------- formatting --

def format_proposal_card(p: dict) -> str:
    """Compact card sent right after a scan. Numbers only, no persuasion."""
    return (
        f"📋 <b>Proposal — {esc(p['action'])} {esc(p['symbol'])}</b>\n"
        f"{esc(p['qty'])} share(s) @ about ${p['price_at_proposal']:.2f}\n"
        f"Rule matched: {esc(p['matched_condition'])}\n\n"
        f"Tap <b>View Detail</b> to see the raw numbers. Nothing happens "
        f"without two taps."
    )


def format_proposal_detail(p: dict, raw_values: dict) -> str:
    lines = [
        f"🔎 <b>{esc(p['action'])} {esc(p['symbol'])} — detail</b>",
        f"Quantity: {esc(p['qty'])} share(s)",
        f"Price when proposed: ${p['price_at_proposal']:.2f}",
        f"Rule: {esc(p['matched_condition'])}",
        f"Why: {esc(p['reason'])}",
        "",
        "<b>Raw values behind this proposal:</b>",
    ]
    for k, v in raw_values.items():
        if isinstance(v, float):
            lines.append(f"• {esc(k)}: {v:,.2f}")
        else:
            lines.append(f"• {esc(k)}: {esc(v)}")
    lines += [
        "",
        "If you tap Confirm, the live price is re-checked against this exact "
        "rule first — if it no longer holds, nothing is placed.",
    ]
    return "\n".join(lines)


def detail_keyboard(proposal_id: str) -> list[list[dict]]:
    return [[{"text": "View Detail", "callback_data": f"detail:{proposal_id}"}]]


def confirm_keyboard(proposal_id: str, action: str) -> list[list[dict]]:
    label = "✅ Confirm Buy" if action == "BUY" else "✅ Confirm Sell"
    return [[{"text": label, "callback_data": f"confirm:{proposal_id}"}],
            [{"text": "✖ Dismiss", "callback_data": f"reject:{proposal_id}"}]]
