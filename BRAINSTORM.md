# Brainstorm — the next phases of trade-scout

> Working document. Built by interviewing the owner; the end product is a
> self-contained prompt/spec good enough to drive future build sessions
> without re-asking these questions. **No code was written during this
> brainstorm.**

## Where the project stands (facts, from the repo)

- Phases 1–3 complete and published: broker client (Robinhood agentic MCP
  endpoint), LLM adapters + fallback (anthropic / openai / **deepseek** already
  supported), Telegram bot with single-owner tenant lock, strategy interview,
  `/scan` → proposal cards, two-tap confirm with live numeric re-verification,
  portfolio / sectors / brief / news views, gated read-only web UI.
- Safety properties are permanent, not settings: two explicit taps per real
  order, live re-check of the original numeric rule before placement, verbatim
  broker errors stop the flow, single-owner tenant lock, no broker credentials
  ever stored (session token lives in process memory only).
- Rule vocabulary today: 9 price-derived metrics (price, pct_change_1d,
  pct_from_5d/20d_high, sma20/50, price_vs_sma20/50_pct, sma50_slope_5d_pct)
  with >=, <=, >, < operators. The interview refuses anything it can't
  numerically re-verify.
- Runs on free tiers: Render (app, sleeps when idle) + Neon (db) + owner's
  Telegram bot + owner's LLM API key. Stocks/ETFs only, no options.

---

## Interview log

### Round 1 — anchoring

- **Topic:** the next feature phase(s) of trade-scout.
- **Current usage:** deployed on Render/Neon and the bot works, but the owner
  has **not confirmed real orders yet**.
- **Doc purpose:** a build prompt for Claude — precise enough that a fresh
  session can build from it without asking the owner anything.

### Round 2 — blocker, candidates, constraints

- **Real blocker:** trust in the proposals. The owner wants *evidence* the
  rules are good before risking money.
- **Candidate directions accepted (ranked in round 3):** scheduled scans +
  alerts · paper-trade / track record · richer signals · position guarding.
- **Constraints confirmed:** free tier only · Telegram-only UX (later revised
  — see round 5) · stocks/ETFs only.

### Round 3 — safety, sequencing, trust bar

- **Safety rails:** frozen for real orders — two-tap confirm, live re-check,
  no-auto-trade stay permanent. Paper mode is exempt from taps: it records
  hypothetical fills automatically because no real order exists.
- **Sequencing:** paper-trade and scheduled scans ship together in the next
  phase; richer signals and position guarding deferred (partially revived by
  round 5's rule-book intelligence ask).
- **Trust bar:** (1) cumulative hypothetical P&L over time, (2) comparison
  against simply holding SPY. Per-rule hit rate and false-fire auditing were
  offered and not selected.

### Round 4 — paper-trade mechanics

- **Exits:** a paper position closes when the owner's own sell rule fires on
  it — the record measures the strategy end-to-end, not just entries.
- **Scan cadence:** market open + close, twice per trading day.
- **Notifications:** silent unless a rule fires (normal proposal card), plus a
  weekly paper-P&L-vs-SPY digest.
- **Go-live bar:** if cumulative paper P&L beats SPY over 4 consecutive
  weeks, the digest says so explicitly.
- **Sizing:** percentage of a **virtual account** — start a virtual balance,
  size each paper position as the strategy's single-position percentage of the
  *current* virtual balance, so the record compounds like a real account.

### Round 5 — scope expansion (owner, free-text)

The owner added, verbatim in intent:

1. Focus on **the intelligence of the rule book** — "add to the intelligence
   of the brain that will trade, valid strategies only."
2. Build a **web app so a friend can have access** too.
3. **Check if DeepSeek can trade as agent in Robinhood.**
4. Available compute: **Ollama (local), Grok, DeepSeek, Gemini** "to do all
   the heavy work."
5. Asked for an honest, in-depth read scoping the entire project.

**Correction (round 7):** point 4 was misread at first as runtime providers
for the app. The owner clarified: these models are the **build-time
workforce** — helpers that do heavy lifting *while building* (the same
pattern BUILD_LOG.md already records with OpenRouter delegation). They do
**not** participate in the running app. The app's runtime brain stays
whatever single provider the owner configures via `LLM_PROVIDER`.

The honest read follows.

### Round 6 — resolving the honest read's open questions

- **Friend access: self-host only.** The friend forks and runs their own free
  instance (supported today, zero code). No guest-view feature, no
  multi-user. The "web app for my friend" ask is satisfied by the template
  nature of the repo itself; the owner's instance stays single-owner.
- **DeepSeek: bulk work only.** (Answered before the round-7 correction, so
  it now applies to the *build-time* workforce: DeepSeek and friends draft
  high-volume, non-personal material; anything safety-relevant is owned and
  reviewed by the coordinator. At runtime the owner simply picks one
  provider via `LLM_PROVIDER`, with the China-hosting privacy note from
  honest-read §1 to weigh if that pick is DeepSeek.)
- **Phase order: B then A.** Rule-book intelligence ships first so the
  4-week track record measures the *improved* strategy from day one.
- **Virtual balance: flat $10,000.** Round benchmark number, easy to compare
  against SPY.
- Spec default (owner's answer was superseded by the round-5 free text, so
  set by me, revisable): on-demand paper record via a `/paper` Telegram
  command plus a read-only web page alongside the existing portfolio view.

---

## Honest read, in depth

### 1. "Can DeepSeek trade as an agent in Robinhood?"

**Short answer: DeepSeek never talks to Robinhood — and that's by design.**
In this architecture no LLM does. The Python app calls Robinhood's agentic
MCP endpoint (`agent.robinhood.com/mcp/trading`) directly; the LLM's only
jobs are translating your interview answers into numeric rules and writing
briefs. Order placement is deterministic code behind the two-tap confirm, and
the rule that triggers a proposal is re-verified *numerically* — the LLM's
opinion is never what places an order.

So the real question decomposes into three honest answers:

- **Can DeepSeek be the brain of this app?** Yes, today, zero code changes:
  `LLM_PROVIDER=deepseek` — the adapter in `src/llm/runtime.py` already
  supports it (`deepseek-chat` default). It is also extremely cheap, which
  matters on a free-tier budget.
- **Can DeepSeek autonomously place trades?** Not in this app, ever — that's
  the frozen safety rail, and it protects you from exactly the failure mode
  you should fear most: a plausible-sounding model hallucinating a trade.
  Robinhood's endpoint itself doesn't know or care which LLM sits upstream;
  the account-level "agentic trading" toggle authorizes the *connection*, not
  a specific model. (Verify Robinhood's current ToS at build time — their
  terms on agentic access may evolve.)
- **Should DeepSeek be the brain?** Caveat to weigh: DeepSeek's API is served
  from China. Your broker credentials never go there (the app never has
  them), but your watchlist, your strategy text, and market context in briefs
  would. If that bothers you, use DeepSeek only for non-personal heavy work
  (bulk summarization, news digestion) and keep strategy translation on
  another provider. This is a privacy call only you can make — flagged as an
  open question below.

### 2. The multi-LLM workforce (Ollama, Grok, DeepSeek, Gemini) — build-time only

**Clarified by the owner: these models help BUILD the project; they do not
participate in the running app.** This matches the pattern BUILD_LOG.md
already documents — delegate a well-specified chunk to a cheap model, then
the coordinator reviews line-by-line and fixes or rewrites before anything is
integrated. The log's own record shows why the review step is non-negotiable:
delegated output came back "structurally complete but thin," with missing
steps and wrong specifics that the coordinator caught and corrected.

Honest rules of engagement for using this workforce in future build sessions:

- **Good delegation targets:** boilerplate (templates, CSS, doc drafts),
  bulk research summaries, first drafts of archetype write-ups, test-case
  enumeration. High volume, low blast radius, easy to verify by reading.
- **Never delegated without coordinator rewrite-level review:** anything in
  the confirm path, order placement, rule evaluation/re-verification, or the
  safety rails. For these, delegate drafts at most; the coordinator owns the
  final code.
- **Verification is the bottleneck, not tokens.** More helper models don't
  shorten the critical path — review discipline does. Delegate to save
  typing, not to save thinking.
- **Ollama practicality:** fine as a local build helper on the owner's
  machine. Grok/DeepSeek/Gemini via API (or OpenRouter, as BUILD_LOG already
  does) for bulk drafting.

**Runtime consequence: none.** The app keeps its existing single-provider
adapter (`LLM_PROVIDER` = anthropic | openai | deepseek). No provider
registry, no routing table, no new runtime adapters are planned.

### 3. Rule-book intelligence — "valid strategies only"

**The most honest sentence in this document: no LLM, and no rule book, can
certify a strategy as profitable.** If "valid" meant "will make money,"
everyone would already be running it. What *can* be built, in increasing
order of value:

1. **Well-formed** (already enforced): every rule maps to a computable
   metric, an operator, and a threshold the app can re-verify. Keep.
2. **Coherent** (new — the strategy critic): after the interview translates
   your words into rules, a critique pass checks the *strategy as a whole*
   and pushes back in plain English before the read-back:
   - no sell rule at all, or a sell rule that can never fire given the buy
     rule (e.g. buy ≥ X and sell ≥ Y with Y unreachable);
   - buy and sell rules simultaneously true at one price;
   - thresholds that fire constantly (pct_change_1d ≥ 0.1%) or never
     (≥ 40%);
   - position sizing that lets a handful of losers halve the account —
     surfaced as a number ("three max-size losses in a row = −X%"), never
     silently changed;
   - no time/trend context (buying every dip in a falling market) — flagged,
     with the vocabulary to fix it.
3. **Archetype-grounded** (new — the strategy library): a curated set of
   documented, boring, defensible archetypes — trend-following (price above
   rising sma50), pullback-in-uptrend (dip from 20d high while above sma50),
   dip-buying with trend filter, plus honest one-paragraph write-ups of when
   each historically works and fails. The interview can say "your words are
   closest to X; here's the standard shape — keep yours or adopt this."
   Curated in the repo (engine/), not generated at runtime, so quality is
   reviewable.
4. **Empirically tested** (the only court that matters): the paper-trade
   phase. The virtual account vs SPY over 4 weeks IS the validity test.
   "Valid strategies only" ultimately means: *the rule book helps you write
   coherent strategies; the paper record tells you which survive.*

To make archetypes expressible, the metric vocabulary needs a modest,
verifiable expansion — each computable from the broker historicals already
fetched (no new data dependency): RSI-14, volume vs 20-day average volume,
ATR-based distance (for stop-style sell rules), pct_from_20d_low, sma200 and
price_vs_sma200_pct (regime filter). Every new metric must keep the
re-verifiability property — if the app can't recompute it live at confirm
time, it doesn't enter the vocabulary.

### 4. Web app for a friend

**This is the biggest architectural and legal item on the list — bigger than
everything else combined.** Today single-owner isn't a limitation, it's a
safety property: one Telegram owner, one broker session in one process's
memory, one strategy. Three honest options, in ascending cost:

- **Option A — friend self-hosts (available today, zero code).** The repo is
  already a template; `tenants/example/` and the phone-only SETUP_GUIDE exist
  precisely for this. Friend forks, deploys their own free Render + Neon +
  bot in ~an hour, connects *their own* Robinhood. Total isolation: their
  money, their instance, their decisions. **Recommended.**
- **Option B — view-only guest on your instance (small, safe build).** Extend
  the existing magic-link web gate with invited viewers: your friend can see
  portfolio, sectors, and the paper track record — read-only, no strategy
  editing, no orders, no broker access. Nice complement to A; pairs naturally
  with the paper-trade web page.
- **Option C — true multi-user on one instance (do not do this casually).**
  Per-user auth, per-user strategies, per-user Robinhood OAuth sessions,
  per-user Telegram binding, per-user budgets. Beyond the heavy rework, the
  honest problems: a free-tier process holding multiple people's broker
  sessions in memory is a fragile and sensitive thing; and **operating a
  service that generates trade proposals for other people drifts toward
  investment-adviser territory** — a regulatory surface that "it's just for
  my friend" does not make disappear. If C is ever wanted, it's its own
  project with its own risk review.

Note: this revises round 2's "Telegram-only UX" — the web surface grows
(paper track record page, possibly guest viewers), but stays read-only.
Orders remain Telegram-only, two-tap.

---

## Decisions

- Deliverable of this session: this file only; no code. Sub-agents idle.
- North star: **build enough evidence/trust to justify the first real order.**
- Constraints: free tier only · stocks/ETFs only · real-order safety rails
  frozen (two-tap, live re-check, no auto-trade, verbatim-error-stops-flow).
- Web surface may grow but stays **read-only**; orders remain Telegram-only.
- Phase A (next build): paper-trade mode + scheduled scans, per round-3/4
  mechanics (virtual compounding account, sell-rule exits, open+close scans,
  fires-only notifications + weekly digest, 4-weeks-beats-SPY go-live signal).
- Phase B: rule-book intelligence — strategy critic + curated archetype
  library + verifiable metric expansion (RSI-14, volume ratio, ATR distance,
  pct_from_20d_low, sma200 family). LLM never gains order authority.
- **Build order: B → A.** Rule-book intelligence first, then paper-trade +
  scheduled scans. There is no Phase C: Ollama/Grok/DeepSeek/Gemini are
  build-time helpers only (round-7 correction) — no runtime provider
  registry, no routing table, no new adapters.
- Friend access: **self-host only** — the friend deploys their own instance
  from this repo. No guest view, no multi-user. Owner's instance stays
  single-owner.
- Build-time delegation: helper models may draft bulk/non-safety material;
  the coordinator reviews everything and owns all safety-relevant code
  (honest-read §2 rules of engagement).
- Virtual paper account starts at **$10,000** flat.
- On-demand paper view: `/paper` command + read-only web page (spec default).

## Build prompts (hand one section to a fresh session, in order)

Each prompt assumes the session reads README.md, BROKER_INTEGRATION.md,
engine/, and progress.json first. Invariants for ALL sessions — restate them
in every plan and never trade them away:

- Real-order rails are frozen: two-tap confirm, live numeric re-check of the
  ORIGINAL rule at confirm time, verbatim broker errors stop the flow, no
  retry on placement, single-owner tenant lock, no broker credentials stored.
- Free tier only (Render sleeps when idle; Neon; owner's own API keys).
- Stocks/ETFs only. LLM output never places, sizes, or times an order.
- Every metric in the rule vocabulary must be recomputable live from broker
  data at confirm time — if it can't be re-verified, it can't be a rule.

### Build 1 — rule-book intelligence (Phase B)

Goal: the interview produces *coherent* strategies and the vocabulary is
rich enough to express standard archetypes. No new data dependencies — every
new metric computes from Robinhood daily historicals already available.

1. **Metric expansion** (src + engine/translator_rules.md, keeping the
   re-verifiability property): rsi14, volume_vs_avg20_pct, atr14_pct
   (ATR as % of price, for stop-style sell rules), pct_from_20d_low,
   sma200 and price_vs_sma200_pct. Update the live re-check path so every
   new metric is recomputed at confirm time exactly like the existing nine.
2. **Strategy critic** (new engine/critic_rules.md + an interview step
   between translation and read-back): checks the strategy as a whole —
   missing/unreachable sell rule; buy and sell simultaneously satisfiable;
   thresholds that fire ~always or ~never; sizing where 3 consecutive
   max-size losses exceed a stated drawdown, surfaced as a number; dip-buying
   with no trend filter. Critic output is plain-English pushback the owner
   can accept or override — it NEVER silently rewrites rules, and an
   explicit override is always allowed and recorded.
3. **Archetype library** (new engine/archetypes.md, curated in-repo, not
   runtime-generated): 4–6 boring, defensible archetypes (trend-following,
   pullback-in-uptrend, dip-buy with regime filter, breakout with volume
   confirmation, …), each with rules in the app's own vocabulary and an
   honest paragraph on when it historically works and fails. The interview
   may offer "your words are closest to X" — adoption is opt-in.
4. Honesty requirement carried into all engine text: the critic and library
   never claim a strategy is profitable — only coherent, expressible, and
   testable. The paper record (Build 2) is the validity court.

### Build 2 — paper-trade mode + scheduled scans (Phase A)

Goal: a self-building track record that tells the owner when the evidence
bar is met.

1. **Virtual account:** starts at $10,000. A paper BUY opens when a buy rule
   fires during any scan (manual or scheduled): filled at the live quote,
   sized as the strategy's single-position percentage of the *current*
   virtual balance, whole shares, recorded automatically (no taps — paper is
   exempt because no real order exists). A paper position closes when the
   owner's sell rule fires on it; P&L realizes into the virtual balance
   (compounding).
2. **Benchmark:** at first paper fill, record a hypothetical $10,000 SPY
   buy-and-hold position; every digest compares the two equity curves.
3. **Scheduled scans:** at market open and close (trading days only,
   holidays respected). Free-tier reality: Render sleeps, so the schedule is
   driven by an external cron ping (e.g. cron-job.org) hitting an
   authenticated endpoint — document this in SETUP_GUIDE with exact steps.
   Scheduled scans reuse the exact `/scan` pipeline and budgets.
4. **Notifications:** silent unless a rule fires (existing proposal card —
   which now ALSO records the paper fill). Weekly digest: virtual account vs
   SPY curve summary, open paper positions, and an explicit line when the
   go-live bar is met: paper P&L has beaten SPY for 4 consecutive weeks.
   The digest states the bar's status every week either way.
5. **Views:** `/paper` Telegram command (balance, open positions, realized
   P&L, vs-SPY delta, weeks-toward-bar) and a read-only web page with the
   trade list and both equity curves, gated like the existing web views.
6. **Paper/real separation, stated everywhere it surfaces:** paper cards and
   pages are visually labeled as paper; confirming a REAL order still works
   exactly as today and is never triggered by paper activity.

*(A former "Build 3 — provider fleet" was removed by the round-7 correction:
Ollama/Grok/DeepSeek/Gemini are build-time helpers, not runtime providers.)*

---

## Round 8 — the cousin's onboarding (2026-07-06)

Context shift from the owner: the repo's purpose is a copy of a working
setup so a **cousin with no PC** can run it; the cousin is stuck at
SETUP_GUIDE step 8 (Connect Robinhood). Investigated and fixed first, ahead
of Builds 1–2.

Verified facts (Robinhood support wording, July 2026):

- Agentic Trading access is a **gradual rollout** — Robinhood emails when an
  account has it. The cousin may simply not have access yet.
- Robinhood **requires a desktop browser** to open the agentic account and
  authenticate the agent — which broke the guide's "no computer needed"
  promise through no fault of the app.
- Setup creates a **separate, funded Agentic account**; this app's
  `agentic_allowed` account lookup matches that design exactly.

Fixes shipped (docs only, no code):

- SETUP_GUIDE: honest intro caveat + "before you start" access check; step 8
  rewritten as a/b/c/d/e with phone-only desktop workarounds (borrowed
  computer, library, "Request desktop site"), the funded-account step, an
  error-message troubleshooting table matching the app's actual error
  strings, and a strongly-recommended free uptime pinger on `/healthz` so
  the in-memory Robinhood session survives Render free-tier sleeps
  (re-login becomes rare instead of every quiet hour).
- BROKER_INTEGRATION: prerequisite section expanded with the same verified
  facts.

Deferred, flagged for a future build decision (touches auth — not done
unilaterally): the OAuth client already registers the `refresh_token` grant
but `handle_oauth_callback` never uses the refresh token, and persisting one
would violate the no-stored-credentials guarantee. If re-login friction
stays painful for the cousin even with the pinger, the honest options are
(a) in-memory refresh-token use only (helps while awake, still dies with the
process) or (b) revisiting the no-persistence guarantee with eyes open.
The keep-awake pinger also becomes required infrastructure for Build 2's
scheduled scans anyway.
