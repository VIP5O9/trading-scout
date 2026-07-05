"""Error policy, in one place.

- TRANSIENT failures (network hiccups, timeouts, 429/5xx) on READ paths retry once
  with a short backoff, then surface to the user.
- GENUINE anomalies (malformed model output after its single retry, broker auth
  failure, order rejection) surface immediately — never silently retried.
- ORDER PLACEMENT is never wrapped in with_retry — a failed order shows the exact
  broker error and stops. Auto-retrying an order could double-buy; nothing in this
  codebase may do it.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx


class TransientHTTPStatus(Exception):
    """Internal marker for retryable HTTP statuses raised by callers that want
    with_retry to handle a 429/5xx response."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException,
                        TransientHTTPStatus)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


async def with_retry(fn: Callable[..., Awaitable[Any]], *args: Any,
                     backoff_seconds: float = 2.0, **kwargs: Any) -> Any:
    """Run fn; on ONE transient failure wait and retry once; then let it raise.
    Use for reads (quotes, history, news, LLM transport). NEVER for orders."""
    try:
        return await fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - filtered right below
        if not is_transient(exc):
            raise
        await asyncio.sleep(backoff_seconds)
        return await fn(*args, **kwargs)
