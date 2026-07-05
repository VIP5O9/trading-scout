"""Model-agnostic LLM adapter: anthropic | openai | deepseek. BYO key.

No sampling parameters are sent to any provider: current Anthropic models
(claude-sonnet-5 and newer) reject non-default temperature/top_p, and recent
OpenAI models restrict them too — prompting is the steering mechanism here.
No retries in this module; src/safety/errors.py owns retry policy.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5-mini",
    "deepseek": "deepseek-chat",
}

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=90.0)
    return _client


class LLMError(Exception):
    """Non-200 or malformed provider response. Includes provider, status, and
    the first 500 chars of the body. Never includes the API key."""


class LLMClient:
    def __init__(self, provider: str, api_key: str, model: str | None = None):
        if provider not in DEFAULT_MODELS:
            raise ValueError(f"Unsupported provider: {provider!r} "
                             "(use anthropic, openai, or deepseek)")
        self.provider = provider
        self._api_key = api_key
        # Constructor arg wins; then LLM_MODEL env; then the provider default.
        self.model = model or os.environ.get("LLM_MODEL") \
            or DEFAULT_MODELS[provider]

    async def complete(self, system: str, messages: list[dict[str, str]],
                       max_tokens: int = 2000) -> str:
        if self.provider == "anthropic":
            return await self._anthropic(system, messages, max_tokens)
        return await self._openai_style(system, messages, max_tokens)

    async def _anthropic(self, system: str, messages: list[dict],
                         max_tokens: int) -> str:
        # Current Anthropic models may run adaptive thinking when `thinking`
        # is omitted; give headroom so thinking doesn't squeeze out the answer,
        # and scan content blocks for the first text block (a thinking block
        # can come first).
        resp = await _http().post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model,
                  "max_tokens": max(max_tokens, 4000),
                  "system": system,
                  "messages": messages})
        data = self._check(resp)
        if data.get("stop_reason") == "refusal":
            raise LLMError("anthropic: the model declined this request "
                           "(stop_reason=refusal)")
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" \
                    and block.get("text"):
                return block["text"]
        raise LLMError("anthropic returned no text content")

    async def _openai_style(self, system: str, messages: list[dict],
                            max_tokens: int) -> str:
        base = ("https://api.openai.com/v1/chat/completions"
                if self.provider == "openai"
                else "https://api.deepseek.com/chat/completions")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        # OpenAI's newer models take max_completion_tokens; DeepSeek keeps
        # the classic max_tokens.
        if self.provider == "openai":
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        resp = await _http().post(
            base,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "content-type": "application/json"},
            json=body)
        data = self._check(resp)
        choices = data.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") \
            if choices else None
        if not content:
            raise LLMError(f"{self.provider} returned empty content")
        return content

    def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code != 200:
            raise LLMError(f"{self.provider} returned HTTP "
                           f"{resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"{self.provider} returned non-JSON body") from exc
