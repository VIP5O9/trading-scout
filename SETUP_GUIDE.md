# Setup Guide for trade-scout

Welcome! Follow these steps to get your own copy running — everything here works
from your phone's web browser. No computer needed. Budget 30–45 minutes.

1. **Fork the repository**
   - Open your mobile browser and go to https://github.com/VIP5O9/trade-scout
   - Tap **Sign up** to create a GitHub account if you don't have one.
   - Once logged in, tap **Fork**, then tap **Create fork**.
   - **Privacy note:** forks of a public repo are public by default. No secrets
     ever live in the repo (all keys go into Render in step 6), but the strategy
     file created later IS visible in a public fork. To keep your watchlist and
     strategy private, change the fork to private: **Settings → General →
     Danger Zone → Change visibility**.

2. **Create a free Neon database**
   - Go to https://neon.tech and tap **Sign up**.
   - Create a project (any name), then copy the **Connection string** shown on
     the dashboard — it starts with `postgresql://`. That whole string is your
     `DATABASE_URL` for step 6.

3. **Get one LLM API key** (pick ONE provider)
   - **Anthropic:** https://console.anthropic.com → **API Keys** → **Create Key**
   - **OpenAI:** https://platform.openai.com → **API keys** → **Create new secret key**
   - **DeepSeek:** https://platform.deepseek.com → **API keys** → **Create API key**
   - Copy the key right away — most consoles show it only once. This usually
     needs a small prepaid balance ($5 is plenty to start). Remember which
     provider you picked: that word (`anthropic`, `openai`, or `deepseek`) is
     your `LLM_PROVIDER` value in step 6.

4. **Get a free Finnhub API key** (for news headlines)
   - Go to https://finnhub.io and tap **Get free API key**. Sign up and copy
     the key from your dashboard.

5. **Create your own Telegram bot**
   - Open Telegram, search for **@BotFather**, and send `/newbot`.
   - Follow its prompts: pick a display name, then a username ending in `bot`.
   - Copy the token BotFather sends (it looks like `123456:ABC-DEF...`).
   - **Warning:** treat this token like a password — anyone who has it can run
     your bot.

6. **Deploy on Render**
   - Go to https://render.com and tap **Sign up**, choosing **GitHub** so Render
     can see your fork.
   - Tap **New +** → **Web Service** → pick your `trade-scout` fork.
   - Choose the **Free** instance type.
   - In the **Environment Variables** section, add ALL FIVE values by exact name:
     - `DATABASE_URL` — the Neon connection string from step 2
     - `LLM_PROVIDER` — `anthropic`, `openai`, or `deepseek`
     - `LLM_API_KEY` — the key from step 3
     - `FINNHUB_API_KEY` — the key from step 4
     - `TELEGRAM_BOT_TOKEN` — the token from step 5
   - Tap **Create Web Service** (some screens label it **Deploy**). Render reads
     `render.yaml` for everything else. The first build takes a few minutes.
   - **Good to know:** the free tier goes to sleep when idle. The first message
     after a quiet period can take up to ~1 minute while it wakes up. That's
     normal, not a bug.

7. **Claim your bot**
   - Open Telegram, search for your bot's username, and send `/start`.
   - The FIRST account to send `/start` becomes the bot's only authorized
     owner, permanently. Do this from your own account right away.

8. **Connect Robinhood**
   - IMPORTANT: **agentic trading** must ALREADY be enabled on your Robinhood
     account before this works. That is done inside Robinhood's own app/site —
     this app cannot and will not do it for you.
   - Send `/connect` to your bot. It sends a Robinhood login link — open it,
     log in on Robinhood's own page, and approve. Your password never touches
     this app, and nothing is stored: after the app has been asleep you'll
     simply be asked to log in again.

9. **Set your strategy and scan**
   - The bot runs a 5-question interview as a normal chat: what to watch, what
     makes you want to buy, what makes you want to sell, how much to risk, and
     how often you'll check in.
   - It reads your strategy back in plain English. Reply **YES** to save, or
     tell it what to change.
   - Then try `/scan`. If one of your rules fires on live prices you'll get a
     proposal card — tap **View Detail** to see the raw numbers, and only a
     separate **Confirm Buy** / **Confirm Sell** tap ever places an order.

Nothing is ever bought or sold without you tapping Confirm yourself, twice, right in this chat.

This app gives you information and proposals, not financial advice — the decision and the risk are yours.
