# Clearbook -- capstone edition of the expense tracker

A multi-user demo of the owner's personal expense tracker, for a capstone
project: at most ~5 users, live for 1-3 days. It started as a copy of the
personal app (a separate repo at ../Personal-Expense-Tracker, which must
never be touched from here).

## Target architecture (being built -- see TODO)

- FastAPI app (Jinja pages, same design) deployed on **Vercel**'s Python
  runtime as one serverless function.
- **Neon Postgres** (free, created from the Vercel dashboard) holds
  everyone's data; `DATABASE_URL` comes from Vercel env.
- **Login**: email + password (scrypt hash), signed session cookie.
  Every query is scoped to the current user's accounts.
- **Plaid Sandbox only, with each user's OWN keys** (entered at
  onboarding, stored encrypted per user). The owner explicitly does not
  want their Plaid keys used -- never put Plaid keys in env or code.
- Sync runs on login and "Sync now" (no background threads on
  serverless).

### TODO (in order)
1. UI design pass: minimal, classy, very simple flow. Done so far:
   signed-out screens (`/landing`, `/login`, `/signup` in `_split.html`
   -- navy brand panel + action panel; logo is the serif "Clearbook"
   text wordmark). Forms show "coming soon" until auth (#2).
2. ~~Users + auth pages; `Account.user_id`; per-user query scoping + tests.~~ Done.
3. ~~Per-user Plaid credentials through plaid_client / plaid_routes /
   plaid_sync.~~ Done: `/setup/plaid` (after sign-up; keys checked with a
   link-token call, secret Fernet-encrypted on `User`), `PlaidCreds` passed
   to every `plaid_client` call, `creds_for(user)` in plaid_sync. Sandbox
   only. Dashboard shows "No accounts to look up" -> Accounts when empty.
4. Onboarding (/welcome): keys -> connect sandbox bank -> auto-created
   accounts with back-filled opening balance -> dashboard; "Load demo
   data" fallback.
5. Serverless fixes: remove `sync_periodically`, NullPool, trusted hosts
   from env, `vercel.json` (entry, maxDuration, security headers).
6. Deploy (owner creates Vercel project + Neon + env vars).

# Working conventions for this repo

Notes for whichever Claude Code session touches this project next, so
decisions made once don't need re-deriving. If something here goes stale,
fix it in the same change that makes it stale.

## Git workflow

- Commit directly to `main`. No feature branches, no PRs -- explicit user
  preference, established after going back and forth on it. Don't
  reintroduce a branch/PR flow without asking first.
- Split commits by logical feature/concern where the diff allows it
  cleanly. When two features touch the same function in an interleaved
  way, it's fine to bundle them into one commit rather than force an
  artificial split -- but verify with `git diff --cached` (not just
  answer-counting through `git add -p`) before trusting a split actually
  landed the right hunks in the right commit.
- Smoke-test before every push: start the server, hit the main routes
  (`/`, `/trends`, `/accounts`, `/manual`), confirm 200s, check
  actual rendered content where a change could plausibly break rendering.
  This has caught real bugs (a 422 on an empty query param, a template
  reading the wrong field) before they reached `main`.
- Never commit `.env` or any `*.db` -- both gitignored. There's no real
  user data here; local development uses a throwaway `dev.db` (SQLite) or
  a Neon dev branch.

## Database

- No migrations (no Alembic) -- `Base.metadata.create_all()` only creates
  *missing tables*. For this short-lived demo, schema changes are applied
  by recreating the (disposable) database rather than hand-written ALTERs.

## App structure

- All routers share one `Jinja2Templates` instance from `app/templating.py`
  (registers a `static_version()` global for cache-busted static assets).
  Don't create a per-router `Jinja2Templates(...)` -- that was the
  original scaffold's pattern and was deliberately consolidated.
- Query params that can arrive as an empty string (e.g. a `<select>`'s
  "All ..." option posting `?account_id=`) must be typed `str | None` in
  the route signature and parsed manually (`int(x) if x else None`) --
  FastAPI 422s on `""` for a declared `int` param. Bit us twice already.

## Money/domain logic

- Credit card balances represent what you *owe* -- a liability, not an
  asset. Income/credits applied directly to a card reduce what's owed
  (subtract), not add to it; this was a real bug once (see git log).
- Refunds and paybacks (`Transaction.is_refund`, INCOME rows only): a
  friend paying you back, a store refund, a card credit. Balances count
  them like income (the money arrived), but every summary/trend goes
  through `spend_amount()` / `earned_amount()` in `services.py`, which
  subtract them from spending in their (expense) category instead of
  counting them as income -- use those helpers, not raw type checks, in
  any new total. The sync flags card credits and Zelle/Venmo/P2P-wallet
  inflows automatically (`_is_refund` in `plaid_sync.py`); pay and
  interest never. Employer reimbursements (BCG) are deliberately *not*
  refunds -- the user treats them as non-living income.
- Categories are a fixed, seeded list (`DEFAULT_EXPENSE_CATEGORIES` /
  `DEFAULT_INCOME_CATEGORIES` in `services.py`) -- no add-category UI yet.
  `seed_default_categories()` only inserts names that don't already exist,
  so adding to the list is safe to apply to an existing database.
- The first 7 categories of each kind (by id) get a dedicated color in
  the Trends stacked charts (expense and income alike); the rest fold into
  a shared "Other" bucket (see `get_category_color_series` in
  `services.py`). The colors (`CATEGORY_COLOR_SLOTS`) come from the app's
  palette family and passed the dataviz validator only for *adjacent*
  slots -- so charts stack series in that fixed order, never sorted by
  amount.

## UI / charts

- Palette: "Dark Green Tropical" -- navy `#13243B`, dark green `#153D35`,
  green `#1D8B65`, teal `#2C9D90`, off-white `#F3F3F1` (tokens at the top
  of `style.css`). User-chosen; don't swap it out.
- Trends charts are drawn client-side by `app/static/charts.js` (plain
  SVG, no chart library -- keep third-party JS out of an app holding bank
  tokens). The route passes plain data via a `|tojson` script blob.
  Every chart needs a real Y-axis; the balance chart starts at $0 and the
  mouse wheel zooms its floor.
- Money is shown with the `money` Jinja filter (`$1,802.43`, `|money(signed=True)`
  for nets). Expenses red (`.amount-expense`), income green
  (`.amount-income`), nets keep positive/negative coloring.
- Dashboard top: one period bar (arrows + "October 2026" month select +
  account select), then a plain-English summary sentence, then the Living
  and Total groups with serif section headings and one-line explainers.
- "Typical month" averages *complete* months only -- the month in progress
  is left out when earlier history exists (`_trailing_average`).
- Nav order is Dashboard > Trends > Accounts > Manual (user-specified).
  "Manual" is the hand-entry form (cash, or anything the sync can't see);
  `/transactions/new` and the old `/import` URLs redirect there.
- "Living" comes first (and is the default) wherever Living/Total toggles
  appear. Toggles are full-width `.segmented` rows (CSS radio + `~`, no
  JS; per-id rules in `style.css`).

## Importing transactions

Plaid sync (`app/plaid_sync.py`, runs in a background thread at launch
and every `SYNC_INTERVAL_HOURS` after, plus a per-account "Sync now") posts straight to the ledger -- no
review queue (the user found the queue too much work once real data
flowed). What keeps that safe:
- Every row a sync touches carries a unique `plaid_transaction_id` (or
  `plaid_pair_transaction_id` for the other side of a transfer), so a
  re-sync is idempotent.
- A new Plaid transaction first tries to *adopt* an unlinked hand-entered
  row with the same amount dated 5 days before to 1 day after it (banks
  post late, never early). Transactions are processed posted-before-pending,
  oldest first, and each claims the *oldest* candidate -- MTA posts several
  days of $3 fares in one batch, and nearest-date matching double-posted
  some. Adopted rows keep their own date/note/category.
- On an account's first sync (cursor None), anything dated before the
  ledger's latest row is adopt-only -- that period was already
  reconciled by hand.
- Pending transactions post with `pending=True` and are updated in place
  when they post. After each sync, Plaid's *posted* balance is compared
  against the ledger minus pending rows.
- The user wants the app to run itself, not to babysit syncing: only
  problems the user must fix (a bank asking to sign in again --
  `NEEDS_USER_ERRORS`) get the red card on Dashboard/Accounts. Other sync
  failures just retry next cycle. A balance gap shows only after it has
  persisted `DRIFT_GRACE_DAYS`, as a quiet line on Accounts. Both pages
  show "Bank data updated X ago" instead.
  Exception: a gap that some subset of pending rows exactly explains isn't
  flagged -- the bank's balance often counts a charge as posted before
  Plaid's feed stops calling it pending, and a later sync resolves it.
- Card payments seen from both checking and the card merge into one
  TRANSFER.

Anything before an account's `opening_balance_date` is ignored. Two
earlier import paths were removed once every bank account was on Plaid:
a Chrome capture extension, and Gemini-parsed statement paste with a
review queue (`PendingImport`; its empty `pending_imports` table is still
in the db, unused). Both are in git history if a non-Plaid bank ever
needs them.

## Demo mode

- Landing page "View a demo" (`POST /demo`) creates a private throwaway
  demo user (`User.is_demo`) with ~4 months of made-up data (`app/demo.py`:
  3 accounts, paychecks, rent, transit, groceries, dining, subscriptions, a
  trip, a non-living one-off, friend paybacks as refunds, monthly card
  payments and ATM top-ups). Generic merchants and randomized amounts --
  **never use the owner's real numbers here.**
- Demo users can't log in (unusable hash), can't set up Plaid or call
  `/plaid/*` (403), see a "made-up data" banner, and "Exit demo" deletes
  their data. Demo users older than 24h are purged when a new demo starts.
- Empty Dashboard: "No accounts connected" + "Set up Plaid" when keys are
  missing, or "Add an account" when keys exist.

## Users and data isolation

- `User` (email lowercased, scrypt hash + salt -- `app/auth.py`); session
  is Starlette's signed cookie `clearbook_session` (SESSION_SECRET, app
  refuses to start without a 32+ char one; HTTPS-only when `VERCEL` is set).
- **Every app route depends on `require_user`**; logged-out GETs redirect
  to `/landing`, other requests get 401.
- **Every data read is scoped to the current user.** Services take a
  required keyword `user_id` and filter transactions through
  `services.owned_account_ids(user_id)`; routes fetch single rows via
  `owned_account()` / `owned_transaction()` (404 if not yours -- same as
  missing, so ids can't be probed). Sync matching (pairing, Venmo top-ups,
  learned categories) stays within the same user's accounts. Any new query
  must do the same -- `tests/test_auth_and_isolation.py` checks it.
- Run tests: `./.venv/bin/python -m pytest -q` (throwaway SQLite).

## Security

This app holds real financial data and Plaid bank credentials. Binding to
127.0.0.1 keeps other *machines* out, but not other *websites* open in the
same browser -- the layers in `app/main.py` exist for that. Don't weaken
them without asking:

- `TrustedHostMiddleware` (127.0.0.1/localhost only) blocks DNS rebinding.
- `reject_cross_site_writes` rejects any POST/PUT/PATCH/DELETE whose
  `Origin` isn't this app -- CSRF protection.
- There is deliberately no CORS middleware, so no other site can read
  responses. It was once `allow_origins=["*"]` (for a since-removed browser
  extension), which let any website read data off the local server.
- Always bind uvicorn to `127.0.0.1`, never `0.0.0.0`.
- Plaid access tokens are Fernet-encrypted before hitting the db
  (`app/token_crypto.py`, key `PLAID_TOKEN_KEY` in `.env`). Never return a
  token (or its ciphertext) to the browser.
- Never disable TLS verification to fix a certificate error -- point the
  client at `certifi.where()` instead (see `app/plaid_client.py`).
- Any value interpolated into inline JS (e.g. an `onsubmit="confirm(...)"`)
  goes through `|tojson` inside a single-quoted attribute -- HTML
  autoescaping alone doesn't protect a JS context.
- `.env` is `chmod 600` locally; secrets on Vercel live in env vars.

## Misc

- The app calls no LLM API. (`google-genai` was used by the removed
  statement parser; `anthropic` was an unused scaffold leftover.)
