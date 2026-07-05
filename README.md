# trade-scout

A personal, self-hosted trade **decision-support** tool you run entirely from your phone.

It watches the stocks/ETFs you choose, checks them against rules **you wrote in your own
words**, and sends you proposals in a private Telegram chat. Nothing is ever bought or
sold until you tap **View Detail** and then **Confirm** — two separate taps, on every
single proposal, with a fresh live-price re-check in between. There is no auto-trade
mode, no batch confirm, and no setting anywhere that changes that.

**What it is not:** an automated trading system, a signal service, or financial advice.
It surfaces information against your own rules; the decision and the risk are yours.

- **Assets:** stocks and ETFs only. No options.
- **Broker:** Robinhood, via Robinhood's own agentic-trading endpoint. Your login happens
  in your own browser with Robinhood directly — this app never sees or stores your
  Robinhood password or any broker credential.
- **Runs on:** free tiers of Render.com (app) + Neon.tech (database), driven by your own
  Telegram bot and your own LLM API key.
- **Setup:** entirely from a phone browser. See [SETUP_GUIDE.md](SETUP_GUIDE.md).

## How it works

1. **Setup interview** (one-time, in Telegram): the bot asks what you want to watch, what
   makes you want to buy, what makes you want to sell, and how much you're willing to
   risk. It reads your answers back in plain English and only saves after you confirm.
2. **Scan on demand**: you send `/scan`. The app pulls live quotes, checks your rules,
   and — only if a rule actually fires on real numbers — sends a proposal card showing
   the exact rule and the exact values behind it.
3. **Two-tap confirm**: tap **View Detail** to see the raw numbers, then tap
   **Confirm Buy** / **Confirm Sell** if you want the order placed. Immediately before
   placing, the app re-fetches the live quote and re-checks the original rule; if it no
   longer holds, it refuses and tells you why.
4. **Views**: `/portfolio` (positions + P&L), `/sectors` (20-day sector momentum),
   `/brief` (LLM market summary, clearly labeled as interpretation), `/news` (headlines
   for your watchlist via Finnhub).

## Repo layout

- `engine/` — the system prompts and rules that drive the LLM (provider-neutral)
- `src/` — FastAPI app: Telegram bot, broker client, LLM adapters, safety rails
- `web/` — read-only status pages (phone-first, gated to the bot owner)
- `tenants/example/` — example of the config + strategy format the interview produces
- `SETUP_GUIDE.md` — phone-only walkthrough from zero to running
- `BROKER_INTEGRATION.md` — how the Robinhood connection works and what is never stored
- `BUILD_LOG.md` — factual record of how this template was built

## Safety properties (permanent, not settings)

- No order without two explicit taps on that specific proposal.
- Live re-verification of the original numeric rule immediately before any order.
- Any broker error during placement is shown verbatim and stops the flow — no retry.
- Only the Telegram account that first ran `/start` can ever talk to the bot or see the
  web views.
- No broker credential, password, or token is ever written to the repo, config, or disk.
