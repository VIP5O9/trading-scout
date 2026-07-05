"""Environment configuration. All secrets come from env vars (set in Render's
dashboard) — nothing here reads or writes credential files."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_provider: str
    llm_api_key: str
    telegram_bot_token: str
    finnhub_api_key: str
    base_url: str            # public https URL of this service (Render provides it)
    robinhood_mcp_url: str

    # Defaults used until the SETUP interview writes tenant config.
    default_max_position_pct: float = 5.0
    default_max_llm_calls_per_day: int = 40


def load_settings() -> Settings:
    missing = [k for k in ("DATABASE_URL", "LLM_PROVIDER", "LLM_API_KEY",
                           "TELEGRAM_BOT_TOKEN", "FINNHUB_API_KEY")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
            + ". Set them in Render's Environment tab (see SETUP_GUIDE.md step 6).")
    provider = os.environ["LLM_PROVIDER"].strip().lower()
    if provider not in ("anthropic", "openai", "deepseek"):
        raise RuntimeError("LLM_PROVIDER must be one of: anthropic, openai, deepseek")
    base_url = (os.environ.get("RENDER_EXTERNAL_URL")
                or os.environ.get("BASE_URL", "http://localhost:8000")).rstrip("/")
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        llm_provider=provider,
        llm_api_key=os.environ["LLM_API_KEY"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        finnhub_api_key=os.environ["FINNHUB_API_KEY"],
        base_url=base_url,
        robinhood_mcp_url=os.environ.get(
            "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"),
    )
