# Setup Guide for trade-scout

Welcome! Follow these steps to get your own copy running — everything here works
from your phone's web browser, **with one exception that is outside this app's
control**: Robinhood requires a desktop browser once, in step 8, to set up
agentic trading on their side. Budget 30–45 minutes plus that one desktop visit.

**Before you start — check two Robinhood things first** (they can take days, so
don't leave them for last):

- You need a regular **Robinhood individual account in good standing**.
- **Agentic Trading access is rolling out gradually** — it may not be available
  on your account yet. Robinhood emails you when you have access; check
  https://robinhood.com/us/en/agentic-trading/ from your Robinhood login. If
  you don't have access yet, you can still do every other step of this guide
  today and finish step 8 when the email arrives.

1. **Fork the repository**
   - Open your mobile browser and go to https://github.com/VIP5O9/trading-scout
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
   - Tap **New +** → **Blueprint** (NOT "Web Service"). This is important: only
     the Blueprint flow reads the `render.yaml` in your fork and fills in the
     build and start commands for you. A plain "Web Service" ignores that file
     and leaves the start command blank/wrong, which fails the deploy.
   - Pick your `trade-scout` fork. Render shows a plan with one free web service
     already configured — tap **Apply** (some screens label it **Create**).
   - Render then asks for the five secret values (they're marked "sync: false" in
     the blueprint, so they're never stored in the repo). Add ALL FIVE by exact
     name:
     - `DATABASE_URL` — the Neon connection string from step 2
     - `LLM_PROVIDER` — `anthropic`, `openai`, or `deepseek`
     - `LLM_API_KEY` — the key from step 3
     - `FINNHUB_API_KEY` — the key from step 4
     - `TELEGRAM_BOT_TOKEN` — the token from step 5
   - The first build takes a few minutes. In the logs you should see `pip install`
     pulling the dependencies, then `Uvicorn running on …`. The health check at
     `/healthz` turns green when it's up.
   - **Good to know:** the free tier goes to sleep when idle. The first message
     after a quiet period can take up to ~1 minute while it wakes up. That's
     normal, not a bug.

7. **Claim your bot**
   - Open Telegram, search for your bot's username, and send `/start`.
   - The FIRST account to send `/start` becomes the bot's only authorized
     owner, permanently. Do this from your own account right away.

8. **Connect Robinhood**

   This is the one step with requirements on Robinhood's side that this app
   cannot change or work around (their rules, verified July 2026):

   - **a. Have access.** Agentic Trading is rolling out gradually; Robinhood
     emails you when your account has it (see "Before you start" above).
   - **b. One-time desktop setup.** Robinhood's own support page says: *"You
     can only open an agentic account and authenticate your agent on a desktop
     device. If you're connecting… on a mobile device, copy the onboarding URL
     and open it in a desktop browser."* No computer of your own? Any of these
     works for the ~10 minutes needed:
     - a family member's or friend's computer — **you** type your Robinhood
       password on Robinhood's own page; nothing about this app is installed
       on their machine and no password ever touches this app;
     - a library computer (log out of Robinhood when done);
     - your phone browser's **"Request desktop site"** mode — sometimes
       enough, not guaranteed.
   - **c. Create and fund the agentic account.** During that desktop
     authentication, Robinhood has you create a **separate Agentic account**
     with its own dedicated budget. That budget is the only money proposals
     from this app can ever trade — pick a number you're comfortable with.
   - **d. Connect the bot.** Send `/connect` to your bot. It replies with a
     Robinhood login link — open it (on the desktop for the first-time setup),
     log in on Robinhood's own page, and approve. Your password never touches
     this app, and nothing is stored. Telegram shows "✅ Robinhood connected"
     when it works.
   - **e. Expect occasional re-logins — and make them rare.** The free tier
     sleeps when idle, and this app deliberately never stores broker tokens,
     so after a sleep you'll be asked to `/connect` again. First-time setup
     definitely needs the desktop; later re-logins may work straight from your
     phone — try the link on your phone before hunting for a computer.
     **Strongly recommended:** set up a free uptime pinger (e.g.
     https://uptimerobot.com or https://cron-job.org) to request
     `https://YOUR-APP.onrender.com/healthz` every 10 minutes. That keeps the
     app awake so your Robinhood session survives instead of dying every quiet
     hour. (One always-on free service fits within Render's free monthly
     hours.)

   **If something fails here, match the message:**
   - *"Login link expired or already used"* → send `/connect` again; each link
     is single-use, and a link goes stale if the app slept after sending it.
   - *"No agentic-trading-enabled Robinhood account was found"* → step **c**
     isn't done: the Agentic account doesn't exist or isn't funded yet, or
     access (step **a**) hasn't been granted to your Robinhood login.
   - *"Could not discover Robinhood's login service"* → almost always means
     agentic access isn't active on your account yet (step **a**).

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
