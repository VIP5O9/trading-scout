"""Postgres (Neon) persistence. Render's local disk is ephemeral, so everything
that must survive a restart lives here. The proposals table is the audit trail:
every proposal ever shown is recorded; status changes update the status column and
append to status_events — the original proposal fields are never overwritten."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id            serial PRIMARY KEY,
    name          text UNIQUE NOT NULL,
    owner_chat_id bigint UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS config (
    tenant_id  int PRIMARY KEY REFERENCES tenants(id),
    yaml       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id          serial PRIMARY KEY,
    tenant_id   int NOT NULL REFERENCES tenants(id),
    version     int NOT NULL,
    strategy_md text NOT NULL,
    rules       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, version)
);
CREATE TABLE IF NOT EXISTS proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         int NOT NULL REFERENCES tenants(id),
    action            text NOT NULL CHECK (action IN ('BUY','SELL')),
    symbol            text NOT NULL,
    qty               int NOT NULL CHECK (qty > 0),
    reason            text NOT NULL,
    confidence        double precision,
    matched_condition text NOT NULL,
    condition_check   jsonb NOT NULL,
    raw_values        jsonb NOT NULL,
    price_at_proposal double precision NOT NULL,
    proposed_at       timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'shown'
        CHECK (status IN ('shown','confirming','confirmed','rejected','expired','stale','error')),
    confirmed_at      timestamptz,
    execution_result  jsonb,
    status_events     jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS news_cache (
    id           bigserial PRIMARY KEY,
    ticker       text NOT NULL,
    headline     text NOT NULL,
    url          text,
    source       text,
    published_at timestamptz,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS news_cache_ticker_idx ON news_cache (ticker, fetched_at);
CREATE TABLE IF NOT EXISTS telegram_sessions (
    chat_id    bigint PRIMARY KEY,
    tenant_id  int REFERENCES tenants(id),
    state      text NOT NULL DEFAULT 'new',
    state_data jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id    bigint PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_calls (
    service text NOT NULL,
    day     date NOT NULL,
    count   int NOT NULL DEFAULT 0,
    PRIMARY KEY (service, day)
);
CREATE TABLE IF NOT EXISTS meta (
    tenant_id int NOT NULL REFERENCES tenants(id),
    key       text NOT NULL,
    value     jsonb,
    PRIMARY KEY (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS web_tokens (
    token      text PRIMARY KEY,
    tenant_id  int NOT NULL REFERENCES tenants(id),
    expires_at timestamptz NOT NULL,
    used       boolean NOT NULL DEFAULT false
);
"""


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def init(self, dsn: str) -> None:
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)

    # ---- tenant / ownership (the tenant lock) ----

    async def get_tenant(self) -> asyncpg.Record | None:
        """Single-tenant deployment: at most one row."""
        return await self.pool.fetchrow("SELECT * FROM tenants ORDER BY id LIMIT 1")

    async def claim_owner(self, name: str, chat_id: int) -> asyncpg.Record | None:
        """First /start wins, atomically. Returns the tenant row if THIS chat now
        owns it (newly claimed or already the owner), else None."""
        async with self.pool.acquire() as con:
            async with con.transaction():
                existing = await con.fetchrow(
                    "SELECT * FROM tenants ORDER BY id LIMIT 1 FOR UPDATE")
                if existing is None:
                    return await con.fetchrow(
                        "INSERT INTO tenants (name, owner_chat_id) VALUES ($1, $2) "
                        "RETURNING *", name, chat_id)
                if existing["owner_chat_id"] == chat_id:
                    return existing
                return None

    # ---- telegram session state ----

    async def get_session(self, chat_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM telegram_sessions WHERE chat_id=$1", chat_id)

    async def set_session(self, chat_id: int, tenant_id: int | None,
                          state: str, state_data: dict) -> None:
        await self.pool.execute(
            """INSERT INTO telegram_sessions (chat_id, tenant_id, state, state_data, updated_at)
               VALUES ($1,$2,$3,$4,now())
               ON CONFLICT (chat_id) DO UPDATE
               SET tenant_id=$2, state=$3, state_data=$4, updated_at=now()""",
            chat_id, tenant_id, state, json.dumps(state_data))

    # ---- webhook idempotency ----

    async def first_time_update(self, update_id: int) -> bool:
        """True exactly once per Telegram update_id (Telegram retries webhooks)."""
        row = await self.pool.fetchrow(
            "INSERT INTO processed_updates (update_id) VALUES ($1) "
            "ON CONFLICT DO NOTHING RETURNING update_id", update_id)
        return row is not None

    # ---- strategy ----

    async def save_strategy(self, tenant_id: int, strategy_md: str,
                            rules: dict, config_yaml: str) -> int:
        async with self.pool.acquire() as con:
            async with con.transaction():
                version = await con.fetchval(
                    "SELECT coalesce(max(version),0)+1 FROM strategy_versions "
                    "WHERE tenant_id=$1", tenant_id)
                await con.execute(
                    "INSERT INTO strategy_versions (tenant_id, version, strategy_md, rules) "
                    "VALUES ($1,$2,$3,$4)", tenant_id, version, strategy_md,
                    json.dumps(rules))
                await con.execute(
                    "INSERT INTO config (tenant_id, yaml, updated_at) VALUES ($1,$2,now()) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET yaml=$2, updated_at=now()",
                    tenant_id, config_yaml)
                return version

    async def get_active_strategy(self, tenant_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM strategy_versions WHERE tenant_id=$1 "
            "ORDER BY version DESC LIMIT 1", tenant_id)

    # ---- proposals (append-only audit trail) ----

    async def insert_proposal(self, tenant_id: int, p: dict) -> str:
        row = await self.pool.fetchrow(
            """INSERT INTO proposals (tenant_id, action, symbol, qty, reason, confidence,
                   matched_condition, condition_check, raw_values, price_at_proposal,
                   proposed_at, status_events)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),
                       jsonb_build_array(jsonb_build_object('at', now()::text, 'status', 'shown')))
               RETURNING id""",
            tenant_id, p["action"], p["symbol"], p["qty"], p["reason"],
            p.get("confidence"), p["matched_condition"],
            json.dumps(p["condition_check"]), json.dumps(p["raw_values"]),
            p["price_at_proposal"])
        return str(row["id"])

    async def get_proposal(self, proposal_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM proposals WHERE id=$1::uuid", proposal_id)

    async def claim_for_confirm(self, proposal_id: str) -> asyncpg.Record | None:
        """Atomically move shown -> confirming. Returns the row only for the ONE
        caller that wins; a double-tap or webhook retry gets None."""
        return await self.pool.fetchrow(
            """UPDATE proposals SET status='confirming',
                   status_events = status_events || jsonb_build_object(
                       'at', now()::text, 'status', 'confirming')
               WHERE id=$1::uuid AND status='shown' RETURNING *""", proposal_id)

    async def finalize_proposal(self, proposal_id: str, status: str,
                                execution_result: dict | None = None,
                                note: str | None = None) -> None:
        await self.pool.execute(
            """UPDATE proposals SET status=$2,
                   confirmed_at = CASE WHEN $2='confirmed' THEN now() ELSE confirmed_at END,
                   execution_result = coalesce($3::jsonb, execution_result),
                   status_events = status_events || jsonb_build_object(
                       'at', now()::text, 'status', $2, 'note', coalesce($4, ''))
               WHERE id=$1::uuid""",
            proposal_id, status,
            json.dumps(execution_result) if execution_result is not None else None,
            note)

    async def proposals_today(self, tenant_id: int) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM proposals WHERE tenant_id=$1 "
            "AND proposed_at::date = (now() at time zone 'utc')::date", tenant_id)

    # ---- daily call budgets (LLM and Finnhub tracked separately by service) ----

    async def count_api_calls_today(self, service: str) -> int:
        n = await self.pool.fetchval(
            "SELECT count FROM api_calls WHERE service=$1 AND day=current_date", service)
        return n or 0

    async def log_api_call(self, service: str) -> None:
        await self.pool.execute(
            "INSERT INTO api_calls (service, day, count) VALUES ($1, current_date, 1) "
            "ON CONFLICT (service, day) DO UPDATE SET count = api_calls.count + 1",
            service)

    # ---- misc per-tenant metadata ----

    async def set_meta(self, tenant_id: int, key: str, value: Any) -> None:
        await self.pool.execute(
            "INSERT INTO meta (tenant_id, key, value) VALUES ($1,$2,$3) "
            "ON CONFLICT (tenant_id, key) DO UPDATE SET value=$3",
            tenant_id, key, json.dumps(value))

    async def get_meta(self, tenant_id: int, key: str) -> Any:
        v = await self.pool.fetchval(
            "SELECT value FROM meta WHERE tenant_id=$1 AND key=$2", tenant_id, key)
        return json.loads(v) if v is not None else None

    # ---- one-time web login tokens (magic links sent via Telegram) ----

    async def create_web_token(self, tenant_id: int) -> str:
        token = secrets.token_urlsafe(32)
        await self.pool.execute(
            "INSERT INTO web_tokens (token, tenant_id, expires_at) "
            "VALUES ($1,$2,$3)", token, tenant_id,
            datetime.now(timezone.utc) + timedelta(minutes=10))
        return token

    async def consume_web_token(self, token: str) -> int | None:
        """One-time use: returns tenant_id if valid and unexpired, else None."""
        row = await self.pool.fetchrow(
            "UPDATE web_tokens SET used=true "
            "WHERE token=$1 AND used=false AND expires_at > now() "
            "RETURNING tenant_id", token)
        return row["tenant_id"] if row else None
