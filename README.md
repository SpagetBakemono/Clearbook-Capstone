# Clearbook

A multi-user expense tracker: log in, connect a (Plaid Sandbox) bank with
your own free Plaid keys, and see your spending, income and balance
trends. Built from a personal expense tracker for a capstone demo; the
target deployment is Vercel + a free Neon Postgres database (see
CLAUDE.md for the architecture and TODO list).

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # fill in PLAID_TOKEN_KEY and SESSION_SECRET
chmod 600 .env
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --host 127.0.0.1 --reload
```

Then open http://127.0.0.1:8000.

## What's built

- **Accounts**: running balances derived from the transaction log (not a
  stored counter), credit cards tracked as what you owe. Editable after
  creation (name, type, opening balance).
- **Transactions**: income / expense / transfer, with category, a
  reimbursable flag (tracked separately from the money actually arriving),
  and an "exclude from living expenses" flag for one-off costs like tuition
  or a deposit. Editable and deletable.
- **Dashboard**: current month's Income/Expenses/Net and Living
  Expenses/Income/Living Net (two rows -- with and without one-off costs
  factored in), each showing this month's actual figure alongside a
  trailing-average "typical month" figure. Spending by category, account
  balances, pending reimbursements, recent transactions. Filterable to a
  single account.
- **Trends** (`/trends`): monthly spending and income as stacked bars by
  category (Living or Total, with a Y-axis and hover breakdown), plus
  total balance over time (daily/weekly/monthly; the axis starts at $0,
  scroll on the chart to zoom in). Filterable by category and date range.
- **Bank sync via Plaid**: link an account from the Accounts page ("Connect
  with Plaid"); new transactions post to the ledger automatically every
  time the app launches (plus a per-account "Sync now"), auto-categorized
  from your own past choices for that merchant or Plaid's category. Pending
  charges show a "pending" badge. Transactions you already entered by hand
  are matched, not duplicated, and each sync checks the bank's balance
  against the app's (a mismatch shows as a red alert). Needs `PLAID_CLIENT_ID`,
  `PLAID_SECRET_SANDBOX` / `PLAID_SECRET_PRODUCTION`, `PLAID_ENV` and
  `PLAID_TOKEN_KEY` in `.env` -- see `.env.example`. Access tokens are
  encrypted at rest.
- **Manual** (`/manual`): add a transaction by hand -- for cash, or
  anything the bank sync can't see.

## Not built yet

- A UI for adding custom categories (currently a fixed, seeded list)
- Budgets per category
- Deployment to Prod (Postgres + hosting)

## Project layout

```
app/
  main.py            FastAPI app, security middleware, startup (tables, seeds, Plaid sync)
  database.py        DB engine/session (SQLite in Dev, set DATABASE_URL for Prod)
  models.py          Account, Category, Transaction, ImportCapture (sync log)
  services.py        Balances, summaries, trends, reimbursements
  templating.py      Shared Jinja2Templates instance (cache-busts static assets)
  plaid_client.py    Plaid API calls (link, exchange, sync, balance) -- no DB
  plaid_sync.py      Plaid -> ledger (auto-post, dedupe, balance check), on startup
  token_crypto.py    Encrypts Plaid access tokens at rest
  routers/           dashboard, accounts, transactions (+ Manual), trends, plaid_routes
  templates/         Jinja2 HTML
  static/            CSS
```
