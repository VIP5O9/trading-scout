# App build — installable PWA with Buy/Sell button (IN PROGRESS)

Paused mid-build. This note is the resume point.

## The goal (confirmed with the owner)

Give a **phone-only** user an app like a self-hosted command center — because
they have no always-on home PC, the same app runs on **their own Render** instead.

Confirmed spec:
- **Agent suggests, user taps Buy/Sell.** DeepSeek + the rules produce suggestions;
  nothing trades until the user taps.
- **Tap Buy/Sell → confirm screen (live price re-check) → order placed** on Robinhood.
- **Installable PWA** is the main interface (home-screen icon, PIN lock like the
  owner's Portfolio OS). **Telegram stays** for push alerts.
- **Self-hosted per person** on Render (free tier is fine because the agent only
  acts on a tap; server wakes on use).

## Architecture decision

The money-placing path is written **once** in `execute_confirmed_order()` in
`src/main.py` (atomic claim → fresh re-verify of the ORIGINAL numeric rule →
place → log, no retry). Both the Telegram Confirm button and the app's Buy button
call it, so the web and chat can never diverge on safety. `running.py` stays
order-free by design; the shared fn lives in `main.py`.

## Auth model (secure on a PUBLIC Render URL)

- Access gate = the existing owner-proven **magic-link cookie session**
  (`/weblink` → `ts_session` cookie in `A.web_sessions`). Unguessable; the public
  URL alone gets you nothing.
- **App PIN** layered on top for quick unlock of the installed app, stored HASHED
  (pbkdf2) in `meta` key `app_pin`, set by the owner via a Telegram `/setpin`
  command. A session id is added to `A.pin_ok` after a correct PIN.
- Placing an order (`/api/confirm`) will require BOTH the cookie session AND
  `sid in A.pin_ok`. So a public visitor without the cookie can do nothing even
  with the right PIN.

## DONE so far (committed)

1. `execute_confirmed_order()` extracted — Telegram renders its structured
   outcome; behavior unchanged. (commit ee0757d)
2. `Database.open_proposals(tenant_id)` — lists the agent's still-'shown'
   suggestions, newest first (for the app list). (this commit)
3. `App.pin_ok: set[str]` app-session state added. (this commit)
4. `main.py` imports: `hashlib`, `FileResponse`. (this commit)

## TODO (remaining, in order)

### Backend (`src/main.py`)
- [ ] PIN helpers: `_hash_pin(pin, salt)` (pbkdf2), `_set_app_pin`, `_pin_is_set`,
      `_check_app_pin` (use `secrets.compare_digest`; `get_meta` already returns a
      dict).
- [ ] Telegram `/setpin <digits>` command: add to the dispatch in `_on_message`
      and a `_cmd_setpin(tenant, chat_id, text)` (owner-locked; min 6 digits;
      delete/echo caution — tell them to set it in a private chat).
- [ ] `_app_ok(request)` helper: returns True iff cookie sid in `A.web_sessions`
      AND sid in `A.pin_ok`. `_session_only_ok` for read (cookie only).
- [ ] JSON API (all return JSON; 401 if not authed):
      - `GET  /api/session` → `{pin_set, session_ok, pin_ok}` (drives lock screen)
      - `POST /api/unlock` `{pin}` → verify → add sid to `A.pin_ok` (rate-limit:
        small backoff / lockout after N fails — PIN is short)
      - `GET  /api/state` → account summary (equity, positions via broker) +
        `open_proposals` suggestions + strategy summary + connected flag +
        `auth_url` when Robinhood login is needed
      - `POST /api/confirm` `{id}` → `execute_confirmed_order` → JSON outcome
        (requires `_app_ok`)
      - `POST /api/dismiss` `{id}` → `finalize_proposal(id, 'rejected')`
      - `POST /api/scan` → run a scan on demand (reuse `_cmd_scan` logic minus TG)
- [ ] Serve the PWA: `GET /app` → `web/app/index.html`; mount assets; serve
      `GET /app/sw.js` with header `Service-Worker-Allowed: /app` (scope!).

### Front-end (new dir `web/app/`)
- [ ] `index.html` — lock screen (PIN) + home (account + suggestion cards with
      Buy/Sell) + confirm modal (shows live re-checked price/cost before placing).
- [ ] `app.js` — fetch `/api/*`, render, unlock, Buy→confirm→`/api/confirm`.
- [ ] `app.css` — dark, mobile-first (can mirror the owner's Portfolio OS style).
- [ ] `manifest.webmanifest` — name, `start_url:/app`, `display:standalone`,
      theme color, icons.
- [ ] `sw.js` — cache the app shell for offline launch (network-first for `/api`).
- [ ] icons — 192/512 PNG (+ maskable) and apple-touch-icon. **Need a way to
      rasterize** (check for PIL/ImageMagick, else ship an SVG + note).

### Docs / deploy
- [ ] SETUP_GUIDE: add "install the app" step (open `/app`, Add to Home Screen,
      set PIN via `/setpin`).
- [ ] Confirm `--workers 1` still holds (it does) — PIN/session state is in-memory.

## Test before calling it done
- `python -m py_compile` the whole `src/` tree.
- The app Buy button and Telegram Confirm both hit `execute_confirmed_order` →
  same outcome.
- A public request to `/api/confirm` without the cookie is rejected.
- Live proof still requires a real deployed `/connect` + a $1 buy.
