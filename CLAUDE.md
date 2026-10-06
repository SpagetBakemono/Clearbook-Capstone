# Clearbook

A multi-user expense tracker, built as a capstone project. Live at
https://clearbook-capstone.vercel.app (auto-deploys on push to `main` of
GitHub `SpagetBakemono/Clearbook-Capstone`).

## Architecture

- FastAPI app with server-rendered Jinja pages, deployed on **Vercel**'s
  Python runtime as one serverless function (`api/index.py`, routed by the
  legacy `builds` + `routes` in `vercel.json` -- a `rewrites` rule handed
  FastAPI the rewritten path and every page 404'd).
- **Neon Postgres** (Vercel Storage, connected with prefix `DATABASE`, so
  `DATABASE_URL` is set) holds everyone's data. psycopg 3 + `NullPool`
  (serverless). `app/database.py` refuses to start on Vercel without
  `DATABASE_URL`; locally it falls back to a throwaway `dev.db` (SQLite).
- **Plaid Sandbox only, with each user's own keys** (entered at
  `/setup/plaid`, secret Fernet-encrypted on `User`). Clearbook has no
  Plaid keys of its own -- never put Plaid keys in env or code. Every
  `plaid_client` call takes the user's `PlaidCreds` (`creds_for(user)`).
- Bank sync runs on login, on "Sync now", and right after "Connect a bank".
  No background threads on Vercel (a local run still syncs every
  `SYNC_INTERVAL_HOURS` in a thread).

# Working conventions

If something here goes stale, fix it in the same change that makes it
stale.

## Git workflow

- Commit directly to `main`; no feature branches or PRs (user preference).
  Pushing deploys.
- Split commits by logical concern; verify with `git diff --cached`.
- Before every push: run the tests, start the server, hit `/landing`, `/`,
  `/trends`, `/accounts`, `/manual` (logged in, e.g. via the demo), and
  check the rendered content where a change could break it.
- Scan the diff for secrets before pushing. The repo is public.
- Never commit `.env`, `vercel-secrets.local.txt` or any `*.db` (all
  gitignored).

## Database

- No migrations: `create_all()` only creates *missing tables*. A new
  column on an existing table needs a manual `ALTER TABLE` on Neon, or
  avoid schema changes (e.g. the `HISTORY_PENDING` opening-date sentinel
  in `plaid_sync.py` was used instead of a new column).

## App structure

- All routers share one `Jinja2Templates` instance from `app/templating.py`
  (globals `static_version()`; filter `money`).
- Query params that can arrive as `""` (a `<select>`'s "All" option) are
  typed `str | None` and parsed manually -- FastAPI 422s on `""` for `int`.

## Users and data isolation

- `User`: email lowercased, scrypt hash + per-user salt (`app/auth.py`).
  The session is Starlette's signed cookie `clearbook_session`, holding the
  user id plus the email (both must match). It needs a `SESSION_SECRET` of
  32+ characters and is HTTPS-only on Vercel.
- **Every app route depends on `require_user`** (Plaid routes on
  `require_real_user`, which excludes demo users). Logged-out page visits
  redirect to `/landing`; other requests get 401.
- **Every data read is scoped to the current user.** Services take a
  required keyword `user_id` and filter through
  `services.owned_account_ids(user_id)`. Single rows come via
  `owned_account()` / `owned_transaction()`, which return 404 if the row
  isn't yours (the same answer as missing, so ids can't be probed). Sync
  matching and Plaid-id lookups stay within the syncing user's accounts.
  Any new query must do the same; `tests/test_auth_and_isolation.py` checks
  it.
- Run tests: `./.venv/bin/python -m pytest -q` (throwaway SQLite).

## Onboarding and demo

- **Sign up**, then `/setup/plaid`:
  - a 4-step walkthrough plus Client ID / Sandbox secret fields;
  - the keys are checked with a link-token call before saving;
  - "Skip for now" is allowed.
- **Empty dashboard:**
  - *without keys:* "Set up Plaid";
  - *with keys:* "Connect a bank";
  - *either way:* "log expenses by hand" (`POST /manual/start` creates a
    Cash account if there's none).
- **"Connect a bank"** (`POST /plaid/connect-bank`):
  - each checking/savings/card account becomes a Clearbook account;
  - it starts with `opening_balance_date = HISTORY_PENDING`;
  - `settle_opening_balance` then back-computes the opening balance from
    the bank's balance once history arrives, so the app agrees with the
    bank.
- **"View a demo"** (`POST /demo`):
  - a private throwaway demo user with ~4 months of made-up data
    (`app/demo.py`): generic merchants and randomized, realistic amounts;
  - capped at 20 per IP per hour and 200 active in total;
  - demo users can't log in or use Plaid;
  - "Exit demo", or signing up or logging in from the demo, deletes the
    demo's data;
  - demos older than 24h are purged.

## Money/domain logic

- Credit card balances are what you *owe* (a liability): credits reduce
  it.
- **Refunds and paybacks** (`Transaction.is_refund`, INCOME rows only):
  - Balances count them like income.
  - Summaries and trends subtract them from spending via `spend_amount()` /
    `earned_amount()` in `services.py`. Use those helpers for any new total.
  - The sync flags card credits and Zelle/Venmo/P2P inflows automatically.
- **Categories** are a fixed, seeded list (`DEFAULT_*_CATEGORIES` in
  `services.py`).
  - In Trends charts, the first 7 per kind get their own color and the rest
    fold into "Other".
  - Series stack in a fixed order, because the palette was validated for
    *adjacent* slots only.
- **"Living" vs "Total":** `exclude_from_living` marks one-offs. The form
  checkbox is worded positively ("Counts as living …").
- **"Typical month"** averages *complete* months only (`_trailing_average`).

## Bank sync rules (`app/plaid_sync.py`)

- **Idempotent:** every synced row carries a unique `plaid_transaction_id`
  (or `plaid_pair_transaction_id` for the other side of a transfer).
- **Adopting hand-entered rows:** a new Plaid transaction first *adopts*
  an unlinked hand-entered row with the same amount, dated 5 days before
  to 1 day after.
  - Processing order is posted before pending, oldest first; each claims
    the *oldest* candidate, because banks batch-post some charges.
- **Pending charges** post with `pending=True` and are updated in place
  when they settle.
- **Card payments** seen from both checking and the card merge into one
  TRANSFER. A bank debit naming a linked wallet (e.g. Venmo) is a transfer
  into it.
- **Problems only the user can fix** (`NEEDS_USER_ERRORS`, e.g. the bank
  wants a re-login) get the red card. Other failures retry quietly.
- **Balance gaps** surface only after `DRIFT_GRACE_DAYS`, unless pending
  rows explain them.

## UI / charts

- **Palette** ("Dark Green Tropical": navy `#13243B`, dark green
  `#153D35`, green `#1D8B65`, teal `#2C9D90`, off-white `#F3F3F1`):
  user-chosen.
- **Light/dark theme:**
  - All colors are tokens at the top of `style.css`: light in `:root`, dark
    in `[data-theme="dark"]` plus the `prefers-color-scheme` fallback.
  - Never hard-code a color in a component.
  - `theme.js` sits in `<head>`; the toggle is `_theme_toggle.html`.
  - Text pairs are checked for WCAG AA. White text only goes on
    `--accent-button`.
- **Charts** are plain SVG drawn by `app/static/charts.js` (no chart
  library -- keep third-party JS out):
  - Data comes via a `|tojson` blob.
  - Category colors are `var(--cat-N)`, applied with `style="fill:..."`.
  - Every chart has a real Y-axis.
- **Forms:** every input and select is 44px tall, flat, with a drawn
  chevron.
- **Nav:** Dashboard > Trends > Accounts > Manual. Living comes first in
  Living/Total toggles.
- The user prefers **few words** in the UI; don't add explanatory copy
  unprompted.

## Security (`app/main.py` and friends -- don't weaken without asking)

- `TrustedHostMiddleware`: localhost plus Vercel's own `VERCEL_*URL`
  hosts plus `ALLOWED_HOSTS`.
- `reject_cross_site_writes`: rejects any write whose `Origin` *host*
  isn't this site, including `Origin: null`. It compares hosts, not
  schemes, because Vercel's proxy may show the app plain http.
- `Referrer-Policy` must stay `same-origin` (or `strict-origin*`). With
  `no-referrer`, browsers send `Origin: null` on form posts and every
  button gets blocked; a test checks this.
- There's no CORS middleware, so other sites can't read responses.
- Security headers on every response: `X-Frame-Options: DENY`, nosniff,
  Permissions-Policy, plus HSTS on Vercel.
- API docs are disabled. Friendly 404/500 pages, with no stack traces.
- Plaid secrets and access tokens are Fernet-encrypted (`PLAID_TOKEN_KEY`)
  and never sent to the browser. `PlaidCreds` masks the secret in its
  repr. Never disable TLS verification (use `certifi.where()`).
- Values inside inline JS go through `|tojson` in a single-quoted
  attribute. No `|safe` / `Markup` on user text. charts.js escapes tooltip
  text.
- Locally, bind uvicorn to `127.0.0.1`. `.env` is `chmod 600`.

## Misc

- The app calls no LLM API.
