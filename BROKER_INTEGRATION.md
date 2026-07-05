# Broker Integration — Robinhood agentic-trading endpoint

## What this app talks to

trade-scout speaks to Robinhood's hosted agentic-trading MCP endpoint
(`https://agent.robinhood.com/mcp/trading`) over HTTPS — quotes, daily
historicals, portfolio/positions, and (only from the two-tap confirm path)
market order placement. The client lives in `src/broker/robinhood.py`.

## Agentic-trading prerequisite

You must enable **agentic trading** on your Robinhood account before any of
this works, and that happens inside Robinhood's own app/site. This repo does
not automate that step and will not work around it. If the login link from
`/connect` errors out, this prerequisite is the first thing to check.

## OAuth browser flow and the no-credential-storage guarantee

Authentication is an OAuth 2.1 browser flow with PKCE: the bot sends you a
Robinhood login link, you sign in on Robinhood's own page, and Robinhood
redirects back to the app with a short-lived code that is exchanged for a
session token.

What this guarantees:

- Your Robinhood username and password are **never** seen by this app.
- No broker credential or token is written to the repo, to application code,
  to config files, to disk, or to the database — the session token exists only
  in the server process's memory.
- The token dies with the process. Render's free tier sleeps when idle, so
  being asked to log in again after a quiet period is **expected behavior**,
  not a bug. There is no env var or config field that persists broker auth.

## Order placement rules (permanent shape of the app)

- Orders happen ONLY through the two-tap confirm in Telegram: tap
  **View Detail** on a proposal, then tap **Confirm Buy** / **Confirm Sell**
  in the detail view. There is no batch confirm and no setting that skips this.
- Immediately before placing (same request, no delay), the app re-fetches the
  live quote and re-evaluates the proposal's ORIGINAL numeric rule — the
  metric/operator/threshold, not the LLM's sentence about it. If it no longer
  holds, the app refuses and shows you the fresh numbers.
- Any broker error during placement (insufficient funds, symbol not found,
  connection error) is shown to you verbatim and the flow STOPS. Order
  placement is never auto-retried — a retry could double-buy.
- Market orders, whole shares, regular hours, good-for-day (`gfd`) time-in-force.
- Orders, portfolio, and positions are scoped to the account flagged
  `agentic_allowed` on your Robinhood login; the app looks this up automatically
  via `get_accounts` after you connect.

## Scope

- **Stocks and ETFs only.** The client wraps no options endpoints, and no
  options order can be expressed anywhere in this codebase.
- **Robinhood-MCP-specific.** This integration is written against Robinhood's
  agentic MCP tools (`get_equity_quotes`, `get_equity_historicals`,
  `get_portfolio`, `get_equity_positions`, `place_equity_order`). A different
  broker would need its own adapter written from scratch — none is included.
- Field names in Robinhood's responses may evolve; the client parses
  defensively (several candidate keys per field) and fails loudly with the raw
  payload rather than guessing.
