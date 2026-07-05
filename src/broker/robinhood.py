"""Thin client for Robinhood's hosted agentic-trading MCP endpoint.

Protocol: MCP over streamable HTTP (JSON-RPC 2.0 POSTs; responses may arrive as
plain JSON or as an SSE event stream). Auth: OAuth 2.1 with PKCE in the USER'S OWN
BROWSER — this process never sees a Robinhood username or password, and the access
token lives only in this object's memory. It is never written to disk, config, the
database, or logs, and it dies with the process (after Render's free tier sleeps,
the user simply logs in again).

Order placement (`place_order`) exists ONLY for the two-tap Telegram confirm path
in src/main.py. Nothing in the scan path imports or calls it. There is no flag,
env var, or config field that changes that.

This client speaks stocks/ETFs only: it wraps no options tools at all.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"

# Map the old span-style windows callers pass to a lookback in days, since the
# real get_equity_historicals tool takes an explicit start_time, not a span.
_SPAN_DAYS = {
    "week": 7, "month": 31, "3month": 93, "6month": 186,
    "year": 372, "5year": 1830,
}


class BrokerError(Exception):
    """Any broker-side failure. Message is safe to show the user verbatim."""


class NeedsAuth(Exception):
    """Raised when a Robinhood login is required. .auth_url is the browser link."""

    def __init__(self, auth_url: str):
        super().__init__("Robinhood login required")
        self.auth_url = auth_url


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _wellknown_candidates(issuer: str) -> list[str]:
    """OAuth 2.0 Authorization Server Metadata URLs to try, in order.

    RFC 8414 §3.1: when the issuer has a path component, the well-known segment
    is INSERTED between the host and that path
    (https://host/.well-known/oauth-authorization-server/path) — it is NOT
    appended after the path. Robinhood's issuer is
    https://agent.robinhood.com/mcp/trading, so the appended form 404s and only
    the inserted form resolves (verified against the live endpoint). We try the
    RFC-correct inserted forms first, then the appended forms for servers that
    use the legacy layout, de-duplicated (both coincide for a root issuer)."""
    u = httpx.URL(issuer)
    netloc = u.netloc.decode("ascii") if isinstance(u.netloc, (bytes, bytearray)) \
        else str(u.netloc)
    origin = f"{u.scheme}://{netloc}"
    path = u.path.rstrip("/")
    urls: list[str] = []
    for wk in ("/.well-known/oauth-authorization-server",
               "/.well-known/openid-configuration"):
        for cand in (origin + wk + path, issuer.rstrip("/") + wk):
            if cand not in urls:
                urls.append(cand)
    return urls


class RobinhoodBroker:
    def __init__(self, mcp_url: str, callback_url: str):
        self.mcp_url = mcp_url
        self.callback_url = callback_url          # https://<app>/oauth/callback
        self._http = httpx.AsyncClient(timeout=45.0, follow_redirects=False)
        self._lock = asyncio.Lock()
        # All auth state is in-memory only — never persisted anywhere.
        self._access_token: str | None = None
        self._account_number: str | None = None   # resolved from get_accounts
        self._session_id: str | None = None
        self._initialized = False
        self._rpc_id = 0
        self._oauth: dict[str, Any] = {}          # discovery + registration state
        self._pending: dict[str, str] = {}        # state -> code_verifier

    @property
    def connected(self) -> bool:
        return self._access_token is not None

    # ------------------------------------------------------------------ RPC --

    async def _post_rpc(self, method: str, params: dict | None,
                        notification: bool = False) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._rpc_id += 1
            body["id"] = self._rpc_id

        resp = await self._http.post(self.mcp_url, json=body, headers=headers)
        if resp.status_code == 401:
            # Token missing/expired: start (or restart) the browser OAuth flow.
            self._access_token = None
            self._initialized = False
            raise NeedsAuth(await self._build_auth_url(resp))
        if resp.status_code >= 400:
            raise BrokerError(
                f"Robinhood endpoint returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}")
        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid
        if notification or resp.status_code == 202 or not resp.content:
            return None

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            payload = self._last_sse_json(resp.text)
        else:
            payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise BrokerError(
                f"Robinhood error {err.get('code')}: {err.get('message')}")
        return payload.get("result") if isinstance(payload, dict) else payload

    @staticmethod
    def _last_sse_json(text: str) -> dict:
        result: dict = {}
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        result = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        if not result:
            raise BrokerError("Robinhood endpoint sent an unreadable stream reply")
        return result

    async def _ensure_initialized(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            await self._post_rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "trade-scout", "version": "1.0"},
            })
            await self._post_rpc("notifications/initialized", None,
                                 notification=True)
            self._initialized = True

    async def _call_tool(self, name: str, arguments: dict) -> Any:
        await self._ensure_initialized()
        result = await self._post_rpc("tools/call",
                                      {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise BrokerError(f"Unexpected reply shape from {name}")
        if result.get("isError"):
            text = self._content_text(result)
            raise BrokerError(f"Robinhood tool {name} failed: {text[:400]}")
        text = self._content_text(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    @staticmethod
    def _content_text(result: dict) -> str:
        parts = result.get("content") or []
        return "\n".join(p.get("text", "") for p in parts
                         if isinstance(p, dict) and p.get("type") == "text")

    # ---------------------------------------------------------------- OAuth --

    async def _build_auth_url(self, resp_401: httpx.Response) -> str:
        """OAuth 2.1 discovery + dynamic client registration + PKCE. Returns the
        URL the user opens in their own browser."""
        www = resp_401.headers.get("www-authenticate", "")
        m = re.search(r'resource_metadata="([^"]+)"', www)
        auth_server = None
        scopes: list[str] = []
        if m:
            r = await self._http.get(m.group(1))
            if r.status_code == 200:
                meta = r.json()
                servers = meta.get("authorization_servers") or []
                auth_server = servers[0] if servers else None
                scopes = meta.get("scopes_supported") or []
        if auth_server is None:
            # Fall back to the MCP host itself as the authorization server.
            auth_server = str(httpx.URL(self.mcp_url).copy_with(path=""))

        if "authorization_endpoint" not in self._oauth:
            discovered = None
            for url in _wellknown_candidates(auth_server):
                r = await self._http.get(url)
                if r.status_code == 200:
                    try:
                        discovered = r.json()
                    except ValueError:
                        continue
                    break
            if not discovered:
                raise BrokerError(
                    "Could not discover Robinhood's login service (no OAuth "
                    f"metadata found for {auth_server}). Check that agentic "
                    "trading is enabled on your Robinhood account "
                    "(see BROKER_INTEGRATION.md).")
            self._oauth = discovered

        if "client_id" not in self._oauth and self._oauth.get("registration_endpoint"):
            r = await self._http.post(self._oauth["registration_endpoint"], json={
                "client_name": "trade-scout (self-hosted)",
                "redirect_uris": [self.callback_url],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })
            if r.status_code not in (200, 201):
                raise BrokerError(
                    f"Robinhood client registration failed: HTTP {r.status_code}")
            self._oauth["client_id"] = r.json().get("client_id")
        if not self._oauth.get("client_id"):
            raise BrokerError("Robinhood login service did not issue a client id")

        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(24))
        self._pending[state] = verifier
        url = httpx.URL(self._oauth["authorization_endpoint"]).copy_merge_params({
            "response_type": "code",
            "client_id": self._oauth["client_id"],
            "redirect_uri": self.callback_url,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            **({"scope": " ".join(scopes)} if scopes else {}),
        })
        return str(url)

    async def get_auth_url(self) -> str | None:
        """Probe the endpoint; returns a login URL if auth is needed, else None."""
        try:
            await self._ensure_initialized()
            return None
        except NeedsAuth as e:
            return e.auth_url

    async def handle_oauth_callback(self, code: str, state: str) -> None:
        verifier = self._pending.pop(state, None)
        if verifier is None:
            raise BrokerError("Login link expired or already used — send /connect "
                              "in Telegram to get a fresh one.")
        r = await self._http.post(self._oauth["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.callback_url,
            "client_id": self._oauth["client_id"],
            "code_verifier": verifier,
        })
        if r.status_code != 200:
            raise BrokerError(f"Robinhood token exchange failed: HTTP {r.status_code}")
        tok = r.json()
        self._access_token = tok.get("access_token")
        if not self._access_token:
            raise BrokerError("Robinhood login reply contained no access token")
        self._initialized = False   # re-handshake with the authenticated session

    # ------------------------------------------------------------- data ----

    @staticmethod
    def _pick(d: dict, *keys: str) -> Any:
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None

    async def _ensure_account(self) -> str:
        """Resolve (and cache) the agentic-trading-enabled account_number, which
        get_portfolio / get_equity_positions / place_equity_order all require.
        The token is per-user; we pick the account flagged agentic_allowed."""
        if self._account_number:
            return self._account_number
        data = await self._call_tool("get_accounts", {})
        rows = data if isinstance(data, list) else (
            self._pick(data, "accounts", "results", "data") or []
            if isinstance(data, dict) else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            num = self._pick(row, "account_number", "account", "number")
            allowed = self._pick(row, "agentic_allowed", "agentic")
            if num and allowed:
                self._account_number = str(num)
                return self._account_number
        raise BrokerError(
            "No agentic-trading-enabled Robinhood account was found. Enable "
            "agentic trading in the Robinhood app first (see "
            "BROKER_INTEGRATION.md), then send /connect again.")

    async def get_quote(self, symbol: str) -> dict:
        """Live quote: {symbol, price, previous_close, updated_at}."""
        data = await self._call_tool("get_equity_quotes", {"symbols": [symbol]})
        rows = data if isinstance(data, list) else (
            self._pick(data, "quotes", "results", "data") or [data]
            if isinstance(data, dict) else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = self._pick(row, "symbol", "ticker")
            if sym and sym.upper() != symbol.upper():
                continue
            price = self._pick(row, "last_trade_price", "last_price", "price",
                               "mark_price", "last_extended_hours_trade_price")
            if price is None:
                continue
            return {
                "symbol": symbol.upper(),
                "price": float(price),
                "previous_close": _maybe_float(self._pick(
                    row, "previous_close", "adjusted_previous_close",
                    "previous_close_price")),
                "updated_at": self._pick(
                    row, "venue_last_trade_time", "updated_at", "last_trade_time"),
            }
        raise BrokerError(f"No usable quote returned for {symbol}")

    async def get_historicals(self, symbol: str, interval: str = "day",
                              span: str = "3month") -> list[dict]:
        """Daily closes, chronologically ascending: [{begins_at, close}, ...]."""
        # The real tool takes an explicit RFC3339 start_time, not a span.
        start = datetime.now(timezone.utc) - timedelta(
            days=_SPAN_DAYS.get(span, 93))
        data = await self._call_tool("get_equity_historicals", {
            "symbols": [symbol], "interval": interval,
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
        rows: list = []
        node: Any = data
        if isinstance(node, dict):
            node = self._pick(node, "results", "historicals", "data") or node
        if isinstance(node, list) and node and isinstance(node[0], dict) \
                and "historicals" in node[0]:
            node = node[0]["historicals"]
        if isinstance(node, dict):
            node = self._pick(node, "historicals", "candles") or []
        if isinstance(node, list):
            rows = node
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            close = _maybe_float(self._pick(r, "close_price", "close", "c"))
            ts = self._pick(r, "begins_at", "timestamp", "date", "t")
            if close is not None and ts is not None:
                out.append({"begins_at": str(ts), "close": close})
        if not out:
            raise BrokerError(f"No usable history returned for {symbol}")
        out.sort(key=lambda x: x["begins_at"])
        return out

    async def get_portfolio(self) -> dict:
        account = await self._ensure_account()
        data = await self._call_tool("get_portfolio",
                                     {"account_number": account})
        return data if isinstance(data, dict) else {"raw": data}

    async def get_positions(self) -> list[dict]:
        account = await self._ensure_account()
        data = await self._call_tool("get_equity_positions",
                                     {"account_number": account})
        if isinstance(data, dict):
            data = self._pick(data, "positions", "results", "data") or []
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------- orders --

    async def place_order(self, symbol: str, side: str, qty: int) -> dict:
        """Market order, whole shares, regular hours. CALLED ONLY from the
        two-tap confirm handler in src/main.py after (1) the user tapped
        'View Detail' then 'Confirm', and (2) the original numeric rule was
        re-verified against a quote fetched in that same request.

        NO retry lives here or in any caller: on any broker error the exact
        message is shown to the user and the flow stops (see main.py)."""
        if side not in ("buy", "sell"):
            raise BrokerError(f"Invalid order side {side!r}")
        if not isinstance(qty, int) or qty < 1:
            raise BrokerError(f"Invalid order quantity {qty!r}")
        account = await self._ensure_account()
        result = await self._call_tool("place_equity_order", {
            "account_number": account,
            "symbol": symbol.upper(),
            "side": side,
            "type": "market",              # real param is `type`, not `order_type`
            "time_in_force": "gfd",        # real values are gfd | gtc, never "day"
            "market_hours": "regular_hours",
            "quantity": str(qty),          # tool expects a string quantity
        })
        return result if isinstance(result, dict) else {"raw": result}


def _maybe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
