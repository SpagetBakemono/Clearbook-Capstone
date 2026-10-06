# Clearbook

**Know where your money goes. Pay $0 to find out.**

Clearbook is a free, simple expense tracker. Connect a bank (through
Plaid, with your own free keys) or log expenses by hand, and see where your
money went: living costs vs one-offs, spending and income by category, and
your balance over time.

**Live:** https://clearbook-capstone.vercel.app. Click **View a demo** to
explore with made-up data, no sign-up needed.

## Using it

1. **Sign up** with an email and password.
2. **Connect Plaid** (optional): follow the 4 steps on screen to get free
   Plaid *Sandbox* keys at dashboard.plaid.com, then paste in the Client ID
   and Secret. Or **Skip for now**.
3. On the empty dashboard, either:
   - **Connect a bank**: in Plaid's window pick any test bank and sign in
     with `user_good` / `pass_good`. Your accounts and their history appear
     automatically. Or
   - **log expenses by hand**: a Cash account is created and you can start
     adding transactions on **Manual**.
4. Explore **Dashboard**, **Trends** (charts) and **Accounts**. The
   sun/moon button switches light and dark.

## Features

- **Dashboard**: the month's living expenses, income and net vs your
  typical month, Total including one-offs, spending by category, accounts,
  and transactions. Filter by month and account.
- **Trends**: stacked monthly spending and income by category, and your
  balance over time (scroll on the chart to zoom).
- **Bank sync** (Plaid Sandbox):
  - Transactions post automatically and are categorized.
  - Pending charges update when they settle.
  - Card payments and refunds are handled so nothing is counted twice.
  - Each sync checks the app's balance against the bank's.
- **Manual entry** for cash, refunds and paybacks, reimbursements, and
  "counts as living".
- **Demo mode**: a private, throwaway account with four months of
  realistic made-up data.

## How it's built

- **App:** Python, FastAPI, server-rendered Jinja templates, and plain
  CSS/JS (charts are hand-drawn SVG; no front-end framework).
- **Hosting:** Vercel (serverless Python).
- **Database:** Neon Postgres, through Vercel Storage.
- **Bank data:** Plaid Sandbox, using each user's own keys. Clearbook has
  no Plaid keys of its own.
- **Security:**
  - Passwords are hashed (scrypt), and each user only ever sees their own
    data.
  - Plaid secrets and tokens are encrypted at rest and never sent to the
    browser.
  - Cross-site request protection and security headers on every response.

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in PLAID_TOKEN_KEY and SESSION_SECRET (commands are in the file)
chmod 600 .env
uvicorn app.main:app --host 127.0.0.1 --reload
```

Open http://127.0.0.1:8000. Without `DATABASE_URL`, it uses a local SQLite
file (`dev.db`).

## Tests

```bash
./.venv/bin/python -m pytest -q
```

The tests run on a throwaway database. They cover:
- sign-up and login;
- password hashing;
- session tampering;
- brute-force lockout;
- cross-site request blocking;
- the Plaid key setup;
- one-click bank connect (with Plaid mocked);
- demo mode;
- the empty-state flows;
- **data isolation between users**, across every page, route and bank
  sync.

## Project layout

```
api/index.py         Vercel entry point
app/
  main.py            FastAPI app, security middleware, error pages
  auth.py            Passwords, sessions, the login gate
  database.py        Postgres on Vercel, SQLite locally
  models.py          User, Account, Transaction, Category, sync log
  services.py        Balances, monthly summaries, trends
  plaid_client.py    Plaid API calls (per-user keys)
  plaid_sync.py      Bank transactions -> ledger (dedupe, transfers, balance check)
  demo.py            "View a demo" data
  routers/           Pages and actions
  templates/         Jinja HTML
  static/            CSS and small JS (charts, theme, connect a bank)
tests/               Automated tests
```
