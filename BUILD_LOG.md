# BUILD_LOG — trade-scout template

Factual record of delegated work and integration fixes. Orchestrator: Claude Fable 5
(coordinator session). Delegates: DeepSeek, Grok, Gemini, OpenRouter via the local
research_pipeline dispatcher clients. Entries are appended as each subtask is integrated.

---

## 2026-07-05 — Scaffold (coordinator-written, not delegated)

**Files:** README.md, .gitignore, requirements.txt, render.yaml, progress.json,
engine/persona.md, engine/core_rules.md, engine/translator_rules.md,
tenants/example/config.yaml, tenants/example/strategy.md

Repo skeleton, engine prompts, and example tenant written directly — sequential
single-file work where delegation adds overhead. Stack locked: Python 3.11+/FastAPI,
asyncpg→Neon, httpx for all external calls, Jinja2 for the read-only web views.
Interface contracts for all delegated modules were fixed at this point (broker method
signatures, db helper names, template variable names) so delegates code against a
stable surface.

---

## 2026-07-05 ~10:35 — Phase 1 core (coordinator-written, not delegated)

**Files:** src/config.py, src/db.py, src/broker/robinhood.py, src/safety/ratelimit.py,
src/safety/errors.py, src/notif/telegram.py, src/modes/running.py, src/modes/setup.py,
src/intel/brief.py, src/main.py

Safety-critical path kept in the coordinator's hands per the orchestration spec:
MCP-over-HTTP broker client with browser OAuth (PKCE + dynamic client registration,
token in memory only), Telegram webhook with tenant lock + update_id idempotency,
5-question interview state machine with mandatory read-back, deterministic metric
engine (LLM proposals numerically vetted against app-computed metrics; qty re-derived
from equity × max_position_pct), and the two-tap confirm: atomic shown→confirming
claim → fresh re-verification of the original metric/op/threshold → place_order →
verbatim-error-and-stop on failure. One later fix: `max_llm_calls_per_day` added to
the config YAML written by setup._save (was omitted in the first version).

---

## 2026-07-05 11:05 — src/llm/runtime.py + src/llm/fallback.py

**Delegated to:** DeepSeek (deepseek-v4-flash via research_pipeline/deepseek_client.py)
**Asked for:** provider adapter (anthropic/openai/deepseek) + validated-JSON fallback
with extract → validate → retry-once-with-error-echoed → refuse.

**Came back:** both files, spec-conformant structure; fallback retry loop and balanced-
brace extractor correct on review.

**Found wrong / changed before integrating:**
- runtime.py sent `temperature` to every provider. Verified against current Anthropic
  API docs: `claude-sonnet-5` rejects non-default sampling params (400), and recent
  OpenAI models restrict them too. REWROTE runtime.py (coordinator): no sampling
  params anywhere; steering is prompt-based.
- Anthropic parser read `content[0]["text"]`. Sonnet 5 runs adaptive thinking when
  `thinking` is omitted, so the first block can be a thinking block. Changed to scan
  for the first text block, added max_tokens floor of 4000 for thinking headroom, and
  added an explicit `stop_reason == "refusal"` branch.
- OpenAI body now uses `max_completion_tokens` (current param name); DeepSeek keeps
  `max_tokens`.
- fallback.py integrated verbatim — no changes needed. Minor known inefficiency (the
  generic-fence branch of extract_json_block rarely matches and falls through to the
  balanced-brace parser, which is correct anyway) left as-is.

---

## 2026-07-05 11:10 — src/intel/sector.py + src/data/finnhub.py

**Delegated to:** Grok (grok-4.3 via research_pipeline/grok_client.py)
**Asked for:** sector ETF 20-day momentum ranking off broker historicals; Finnhub
company-news client with 1h Neon cache, separate daily cap, on-demand refresh only.

**Came back:** both files matching the locked interfaces; 20-day return indexing
(data[-21] vs data[-1]) correct; cache-first logic and cap check per spec.

**Found wrong / changed before integrating:**
- sector.py's blanket `except` swallowed NeedsAuth, which would render a Robinhood
  login prompt as 8 "error" rows. Added a re-raise for NeedsAuth.
- finnhub.py put the API token in the URL string, which could leak into httpx
  exception messages. Moved to `params=`, and wrapped transport errors in a sanitized
  FinnhubError (type name only, no URL).
- `it["datetime"]` would KeyError on items missing a timestamp. Guarded with .get()
  and a now() fallback; also skip non-dict items.

---

## 2026-07-05 11:12 — web/ templates + stylesheet

**Delegated to:** Gemini (via research_pipeline/gemini_client.py)
**Asked for:** phone-first dark read-only Jinja2 views (base/index/portfolio/sectors/
denied + style.css), no JS, no CDN, no mutating elements, exact template vars.

**Came back:** all 6 files. Review found: proper `{% extends %}` inheritance, no
`|safe` anywhere (autoescape stays effective), no `<script>`/`<form>`/fetch, footer
disclaimer present, pre-wrap handling for the strategy summary as requested.

**Changed before integrating:** nothing — integrated verbatim.

---

## 2026-07-05 11:18 — SETUP_GUIDE.md + BROKER_INTEGRATION.md

**Delegated to:** OpenRouter default model (via research_pipeline/openrouter_client.py)
**Asked for:** phone-only numbered walkthrough per the spec (exact buttons, 5 env
vars, BotFather flow, fork-privacy note, closing sentences) + broker doc (prerequisite,
OAuth, no-storage guarantee, two-tap, Robinhood-specific statement).

**Came back:** structurally complete but thin (~1.4k tokens for both files). All 9
steps, five env vars, and both closing sentences were present and correct.

**Found wrong / changed before integrating (coordinator rewrote both, keeping the
delegate's structure and privacy-note text):**
- LLM key steps said "find the key page" — replaced with the actual page names per
  provider (API Keys / API keys / Create API key).
- Missing the Render cold-start expectation note — added.
- Missing concrete env-var value hints (which string goes in DATABASE_URL and
  LLM_PROVIDER) — added.
- Step 8 didn't name the /connect command the bot actually uses — added.
- Closing sentences were merged into one paragraph — split into the two required
  standalone sentences.
- BROKER_INTEGRATION.md (15 lines) covered all required points but omitted the
  endpoint URL, the tool names, PKCE, and the defensive-parsing caveat — expanded.
