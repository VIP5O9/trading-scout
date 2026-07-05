from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from typing import Any

import httpx


class FinnhubError(Exception):
    """Base error for Finnhub client."""


class FinnhubCapError(FinnhubError):
    """Daily Finnhub call cap reached — message must say the cap and that it resets tomorrow."""


class FinnhubClient:
    """Finnhub company-news client with 1-hour Postgres cache."""

    def __init__(self, api_key: str, db: Any) -> None:
        self.api_key = api_key
        self.db = db

    async def company_news(self, symbol: str, days_back: int = 7) -> list[dict]:
        """Return up to 8 cached or fresh news items; raises on cap or HTTP error."""
        # 1. cache check (1h TTL)
        rows = await self.db.pool.fetch(
            "SELECT headline, url, source, published_at FROM news_cache "
            "WHERE ticker=$1 AND fetched_at > now() - interval '1 hour' "
            "ORDER BY published_at DESC LIMIT 8",
            symbol,
        )
        if rows:
            return [
                {
                    "headline": r["headline"],
                    "url": r["url"],
                    "source": r["source"],
                    "published_at": r["published_at"].isoformat(),
                }
                for r in rows
            ]

        # 2. cap check
        cap = int(os.environ.get("FINNHUB_DAILY_CAP", "150"))
        used = await self.db.count_api_calls_today("finnhub")
        if used >= cap:
            raise FinnhubCapError(
                f"Daily Finnhub call cap ({cap}) reached — resets tomorrow."
            )

        # 3. fetch
        today = datetime.now(timezone.utc).date()
        from_d = (today - timedelta(days=days_back)).isoformat()
        to_d = today.isoformat()
        # Token goes in params, never in a URL string that could leak into an
        # exception message or log line.
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": symbol, "from": from_d, "to": to_d,
                            "token": self.api_key})
        except httpx.HTTPError as exc:
            raise FinnhubError(
                f"network error reaching Finnhub ({type(exc).__name__})"
            ) from exc
        if resp.status_code != 200:
            body = resp.text[:300]
            raise FinnhubError(f"HTTP {resp.status_code}: {body}")
        await self.db.log_api_call("finnhub")
        items = resp.json()

        # 4. store newest 8 (transaction)
        to_store = []
        for it in items:
            if not isinstance(it, dict) or not it.get("headline"):
                continue
            ts = it.get("datetime")
            to_store.append(
                {
                    "headline": it["headline"],
                    "url": it.get("url", ""),
                    "source": it.get("source", ""),
                    "published_at": (
                        datetime.fromtimestamp(ts, tz=timezone.utc)
                        if ts else datetime.now(timezone.utc)
                    ),
                }
            )
            if len(to_store) == 8:
                break

        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM news_cache WHERE ticker=$1", symbol
                )
                for item in to_store:
                    await conn.execute(
                        "INSERT INTO news_cache(ticker, headline, url, source, "
                        "published_at, fetched_at) VALUES($1,$2,$3,$4,$5,now())",
                        symbol,
                        item["headline"],
                        item["url"],
                        item["source"],
                        item["published_at"],
                    )

        # 5. return
        return [
            {
                "headline": i["headline"],
                "url": i["url"],
                "source": i["source"],
                "published_at": i["published_at"].isoformat(),
            }
            for i in to_store
        ]
