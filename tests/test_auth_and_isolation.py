"""
Sign up / log in / log out, and -- the part that matters most -- that one
user can never see or change another user's data.

Run:  ./.venv/bin/python -m pytest -q
Uses a throwaway SQLite file; never touches dev.db or a real database.
"""
import os
import re
import sqlite3
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp, "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SESSION_SECRET"] = "test-secret-" + "x" * 40
from cryptography.fernet import Fernet  # noqa: E402

os.environ["PLAID_TOKEN_KEY"] = Fernet.generate_key().decode()  # throwaway, per run

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BASE = "http://127.0.0.1"  # TrustedHostMiddleware only allows localhost names


@pytest.fixture(scope="module")
def started():
    with TestClient(app, base_url=BASE) as c:  # runs startup (tables, seeds)
        yield c


def client():
    return TestClient(app, base_url=BASE)


def signup(c, email, password="correct-horse-1"):
    return c.post(
        "/signup",
        data={"email": email, "password": password, "confirm": password},
        follow_redirects=False,
    )


def add_account(c, name, type_="checking", balance="100"):
    r = c.post(
        "/accounts",
        data={"name": name, "type": type_, "opening_balance": balance, "opening_balance_date": "2026-09-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return int(re.findall(r"/accounts/(\d+)/edit", c.get("/accounts").text)[-1])


def add_txn(c, account_id, note, amount="12.34", type_="expense"):
    r = c.post(
        "/transactions",
        data={"type": type_, "date": "2026-09-15", "amount": amount, "account_id": account_id,
              "category_id": "1", "note": note, "counts_as_living": "1"},
        follow_redirects=False,
    )
    return r


def txn_id_by_note(note):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute("select id from transactions where note=?", (note,)).fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------- auth

def test_logged_out_pages_redirect_to_landing(started):
    c = client()
    for path in ["/", "/trends", "/accounts", "/manual"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/landing", path
    for path in ["/landing", "/login", "/signup"]:
        assert c.get(path).status_code == 200, path


def test_signup_login_logout(started):
    c = client()
    r = signup(c, "Alice@Example.com")
    assert r.status_code == 303 and r.headers["location"] == "/setup/plaid"  # next: Plaid keys
    assert c.get("/").status_code == 200
    assert "alice@example.com" in c.get("/").text  # shown lowercased in the top bar

    c.post("/logout", follow_redirects=False)
    assert c.get("/", follow_redirects=False).headers["location"] == "/landing"

    bad = c.post("/login", data={"email": "alice@example.com", "password": "wrong-password"})
    assert bad.status_code == 400 and "Email or password is incorrect" in bad.text
    unknown = c.post("/login", data={"email": "nobody@example.com", "password": "whatever-123"})
    assert "Email or password is incorrect" in unknown.text  # same message: no account probing

    ok = c.post("/login", data={"email": "ALICE@example.com", "password": "correct-horse-1"},
                follow_redirects=False)
    assert ok.status_code == 303 and c.get("/").status_code == 200


def test_signup_validation(started):
    c = client()
    assert "valid email" in signup(c, "not-an-email").text
    assert "at least 8" in c.post("/signup", data={"email": "b@x.co", "password": "short", "confirm": "short"}).text
    assert "match" in c.post("/signup", data={"email": "b@x.co", "password": "longenough1", "confirm": "different1"}).text
    signup(client(), "dup@example.com")
    assert "already exists" in signup(client(), "DUP@example.com").text


def test_password_stored_only_as_hash(started):
    signup(client(), "hash@example.com", "plaintext-check-99")
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("select password_hash, password_salt from users where email='hash@example.com'").fetchone()
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    assert row and len(row[0]) == 128 and len(row[1]) == 32
    assert "plaintext-check-99" not in dump


def test_tampered_cookie_is_logged_out(started):
    c = client()
    signup(c, "tamper@example.com")
    cookie = c.cookies.get("clearbook_session")
    assert cookie
    forged = client()
    forged.cookies.set("clearbook_session", cookie[:-4] + ("AAAA" if not cookie.endswith("AAAA") else "BBBB"))
    assert forged.get("/", follow_redirects=False).headers["location"] == "/landing"


def test_too_many_failed_logins(started):
    signup(client(), "brute@example.com")
    c = client()
    for _ in range(10):
        c.post("/login", data={"email": "brute@example.com", "password": "nope-nope-1"})
    r = c.post("/login", data={"email": "brute@example.com", "password": "correct-horse-1"})
    assert "Too many attempts" in r.text


def test_cross_site_post_blocked(started):
    r = client().post("/signup", data={"email": "x@x.co", "password": "aaaaaaaa1", "confirm": "aaaaaaaa1"},
                      headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ----------------------------------------------------------- isolation

def test_users_never_see_or_change_each_others_data(started):
    a, b = client(), client()
    signup(a, "a@iso.test")
    signup(b, "b@iso.test")
    a_acct = add_account(a, "Alpha Checking", balance="1000")
    b_acct = add_account(b, "Bravo Checking", balance="5000")
    assert add_txn(a, a_acct, "ALPHA-SECRET-LUNCH").status_code == 303
    assert add_txn(b, b_acct, "BRAVO-SECRET-DINNER", amount="77.77").status_code == 303
    b_txn = txn_id_by_note("BRAVO-SECRET-DINNER")

    # Nothing of B's shows on any of A's pages.
    for path in ["/", "/accounts", "/trends", "/manual", "/?month=2026-09"]:
        page = a.get(path).text
        assert "BRAVO" not in page and "Bravo Checking" not in page, path
        assert "77.77" not in page, path
    assert "Alpha Checking" in a.get("/accounts").text
    assert "987.66" in a.get("/").text  # 1000 opening - 12.34 of A's own spending, nothing of B's

    # A can't open, edit, delete or close out B's transaction...
    assert a.get(f"/transactions/{b_txn}/edit").status_code == 404
    edit = {"type": "expense", "date": "2026-09-15", "amount": "1", "account_id": a_acct, "note": "hacked"}
    assert a.post(f"/transactions/{b_txn}/edit", data=edit).status_code == 404
    assert a.post(f"/transactions/{b_txn}/delete").status_code == 404
    assert a.post(f"/transactions/{b_txn}/mark-reimbursed").status_code == 404
    # ...or file a transaction on B's account, or a transfer into it.
    assert add_txn(a, b_acct, "SNEAKY").status_code == 404
    transfer = {"type": "transfer", "date": "2026-09-15", "amount": "5", "account_id": a_acct,
                "to_account_id": b_acct, "note": "SNEAKY2"}
    assert a.post("/transactions", data=transfer).status_code == 404

    # ...or touch B's account.
    assert "Bravo" not in a.get(f"/accounts/{b_acct}/edit").text
    a.post(f"/accounts/{b_acct}/edit", data={"name": "pwned", "type": "checking",
                                              "opening_balance": "0", "opening_balance_date": "2026-09-01"})
    a.post(f"/accounts/{b_acct}/plaid/disconnect")

    con = sqlite3.connect(DB_PATH)
    try:
        assert con.execute("select name, opening_balance from accounts where id=?", (b_acct,)).fetchone() == ("Bravo Checking", 5000)
        assert con.execute("select note, amount from transactions where id=?", (b_txn,)).fetchone() == ("BRAVO-SECRET-DINNER", 77.77)
        assert con.execute("select count(*) from transactions where note like 'SNEAKY%'").fetchone()[0] == 0
    finally:
        con.close()

    # And B still sees all of their own data.
    assert "BRAVO-SECRET-DINNER" in b.get("/?month=2026-09").text
    assert "Bravo Checking" in b.get("/accounts").text


def test_bank_sync_never_matches_across_users(started):
    """Sync matching (card-payment pairing, Venmo top-ups, learned
    categories) must only ever look at the same user's accounts."""
    from datetime import date
    from decimal import Decimal as D
    from unittest.mock import patch

    import app.plaid_sync as ps
    from app.database import SessionLocal
    from app.models import Account, AccountType, Transaction, TransactionType, User

    db = SessionLocal()
    ua = User(email="sync-a@iso.test", password_hash="x", password_salt="y",
              plaid_client_id="cid-a", plaid_secret="enc-a")
    ub = User(email="sync-b@iso.test", password_hash="x", password_salt="y",
              plaid_client_id="cid-b", plaid_secret="enc-b")
    db.add_all([ua, ub]); db.commit()
    a_chk = Account(user_id=ua.id, name="A Checking", type=AccountType.CHECKING, opening_balance=D(0),
                    opening_balance_date=date(2026, 9, 1), plaid_access_token="tok", plaid_cursor="c")
    b_card = Account(user_id=ub.id, name="B Card", type=AccountType.CREDIT_CARD, opening_balance=D(0),
                     opening_balance_date=date(2026, 9, 1), plaid_access_token="tok", plaid_cursor="c")
    b_venmo = Account(user_id=ub.id, name="Venmo", type=AccountType.CHECKING, opening_balance=D(0),
                      opening_balance_date=date(2026, 9, 1), plaid_access_token="tok", plaid_cursor="c")
    db.add_all([a_chk, b_card, b_venmo]); db.commit()
    # B's card already shows a $55.55 payment credit, and B filed "SHARED MERCHANT" under Travel.
    db.add(Transaction(date=date(2026, 9, 20), amount=D("55.55"), type=TransactionType.INCOME,
                       account_id=b_card.id, plaid_transaction_id="b-card-credit", note="PAYMENT"))
    db.add(Transaction(date=date(2026, 9, 1), amount=D("9.00"), type=TransactionType.EXPENSE,
                       account_id=b_venmo.id, note="SHARED MERCHANT", category_id=17))
    db.commit()

    def t(pid, amt, merchant, prim, det=None):
        return dict(plaid_id=pid, pending_plaid_id=None, date=date(2026, 9, 21), amount=D(amt), outflow=True,
                    merchant=merchant, pending=False, category_primary=prim, category_detailed=det)

    added = [t("a-cardpay", "55.55", "CARD PAYMENT", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
             t("a-venmo", "20.00", "Venmo", "TRANSFER_OUT"),
             t("a-shared", "9.00", "SHARED MERCHANT", "FOOD_AND_DRINK")]
    res = dict(added=added, modified=[], removed=[], next_cursor="c2")
    with patch.object(ps, "sync_transactions", return_value=res), \
         patch.object(ps, "get_balance", return_value=None), \
         patch.object(ps, "decrypt_token", return_value="tok"):
        ps.sync_plaid_account(db, a_chk)

    def by(pid):
        return db.query(Transaction).filter_by(plaid_transaction_id=pid).one()

    pay = by("a-cardpay")
    assert pay.account_id == a_chk.id and pay.to_account_id is None, "paired with another user's card"
    venmo = by("a-venmo")
    assert venmo.to_account_id != b_venmo.id, "treated as a top-up of another user's Venmo"
    assert by("a-shared").category_id != 17, "learned a category from another user's history"
    b_credit = db.query(Transaction).filter_by(note="PAYMENT", account_id=b_card.id).one()
    assert b_credit.type == TransactionType.INCOME and b_credit.plaid_pair_transaction_id is None
    db.close()


# ------------------------------------------------------------ plaid keys

FAKE_CLIENT_ID = "a1b2c3d4e5f6a7b8c9d0e1f2"
FAKE_SECRET = "0123456789abcdef0123456789abcd"


def test_empty_dashboard_points_to_accounts(started):
    c = client()
    signup(c, "empty@example.com")
    page = c.get("/").text
    assert "No accounts connected" in page


def test_plaid_keys_saved_encrypted_after_check(started):
    from unittest.mock import patch

    import app.routers.auth as auth_routes

    c = client()
    signup(c, "keys@example.com")
    assert "Connect Plaid" in c.get("/setup/plaid").text
    assert "add your Plaid keys" in c.get("/accounts").text

    # Garbage is refused before Plaid is even asked.
    assert "look like a Plaid key" in c.post("/setup/plaid", data={"client_id": "x", "secret": "y"}).text

    # Keys Plaid rejects are not saved.
    class Rejected(Exception):
        body = '{"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"}'
    with patch.object(auth_routes, "create_link_token", side_effect=Rejected()):
        r = c.post("/setup/plaid", data={"client_id": FAKE_CLIENT_ID, "secret": FAKE_SECRET})
    assert r.status_code == 400 and "accept those keys" in r.text and FAKE_SECRET not in r.text

    # Keys Plaid accepts are saved -- secret encrypted -- then on to the dashboard.
    with patch.object(auth_routes, "create_link_token", return_value="link-sandbox-ok") as called:
        r = c.post("/setup/plaid", data={"client_id": FAKE_CLIENT_ID, "secret": FAKE_SECRET},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert called.call_args.args[0].secret == FAKE_SECRET  # the user's own keys were the ones tested

    con = sqlite3.connect(DB_PATH)
    try:
        cid, stored = con.execute("select plaid_client_id, plaid_secret from users where email='keys@example.com'").fetchone()
    finally:
        con.close()
    assert cid == FAKE_CLIENT_ID and stored and stored != FAKE_SECRET and FAKE_SECRET not in stored
    from app.token_crypto import decrypt_token
    assert decrypt_token(stored) == FAKE_SECRET

    # The secret never comes back to the browser.
    for path in ["/setup/plaid", "/accounts", "/"]:
        assert FAKE_SECRET not in c.get(path).text, path
    assert "Change keys" in c.get("/accounts").text


def test_no_keys_means_no_plaid_calls(started):
    """Without the user's own keys there is nothing to call Plaid with --
    Clearbook has no keys of its own to fall back on."""
    c = client()
    signup(c, "nokeys@example.com")
    r = c.post("/plaid/create-link-token")
    assert r.status_code == 400 and r.json()["setup_url"] == "/setup/plaid"


# ------------------------------------------------------------------ demo

def test_view_demo_gives_a_full_private_dashboard(started):
    from datetime import date

    from dateutil.relativedelta import relativedelta

    from app.database import SessionLocal
    from app.models import User
    from app.services import get_all_balances, get_month_summary

    a, b = client(), client()
    r = a.post("/demo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    b.post("/demo")

    page = a.get("/").text
    assert "made-up data" in page and "Exit demo" in page
    for name in ["Everyday Checking", "Rewards Card", "Cash"]:
        assert name in a.get("/accounts").text
    assert a.get("/trends").status_code == 200

    db = SessionLocal()
    demos = db.query(User).filter_by(is_demo=True).order_by(User.id).all()
    ua, ub = demos[-2], demos[-1]
    assert len(get_all_balances(db, user_id=ua.id)) == 3
    today = date.today()
    first = date(today.year, today.month, 1)
    for back in range(1, 4):  # every past month is filled in
        m = first - relativedelta(months=back)
        s = get_month_summary(db, m.year, m.month, user_id=ua.id)
        assert s["expenses"] > 1000 and s["income"] > 3000, m
    totals = [get_month_summary(db, (first - relativedelta(months=k)).year,
                                (first - relativedelta(months=k)).month, user_id=ua.id) for k in range(4)]
    assert any(t["living_expenses"] != t["expenses"] or t["living_income"] != t["income"] for t in totals)
    # each visitor's demo is their own
    a_ids = {acc.id for acc, _ in get_all_balances(db, user_id=ua.id)}
    b_ids = {acc.id for acc, _ in get_all_balances(db, user_id=ub.id)}
    assert a_ids and b_ids and not (a_ids & b_ids)
    db.close()

    # no Plaid in the demo
    assert a.get("/setup/plaid", follow_redirects=False).headers["location"] == "/"
    assert a.post("/plaid/create-link-token").status_code == 403
    # and nobody can log into a demo account
    assert "incorrect" in client().post("/login", data={"email": ua.email, "password": "anything-1"}).text


def test_demo_cleanup(started):
    from datetime import datetime, timedelta

    from app.database import SessionLocal
    from app.demo import create_demo_user, delete_stale_demo_users
    from app.models import Account, Transaction, User

    db = SessionLocal()
    old = create_demo_user(db)
    old.created_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    old_id = old.id
    real = User(email="real-person@example.com", password_hash="x", password_salt="y")
    db.add(real); db.commit()
    assert delete_stale_demo_users(db) >= 1
    assert db.get(User, old_id) is None
    assert db.query(Account).filter_by(user_id=old_id).count() == 0
    assert db.get(User, real.id) is not None  # real users are never touched
    db.close()

    # Exit demo deletes that demo right away
    c = client()
    c.post("/demo")
    db = SessionLocal()
    mine = db.query(User).filter_by(is_demo=True).order_by(User.id.desc()).first().id
    db.close()
    c.post("/logout")
    db = SessionLocal()
    assert db.get(User, mine) is None
    assert db.query(Transaction).join(Account, Transaction.account_id == Account.id).filter(Account.user_id == mine).count() == 0
    db.close()


def test_empty_states_depend_on_plaid_keys(started):
    from unittest.mock import patch

    import app.routers.auth as auth_routes

    c = client()
    signup(c, "empty2@example.com")
    page = c.get("/").text
    assert "No accounts connected" in page and "set up yet" in page and 'href="/setup/plaid"' in page
    with patch.object(auth_routes, "create_link_token", return_value="ok"):
        c.post("/setup/plaid", data={"client_id": FAKE_CLIENT_ID, "secret": FAKE_SECRET})
    page = c.get("/").text
    assert "No accounts connected" in page and "set up yet" not in page and "Add an account" in page


def test_browser_form_posts_are_not_blocked(started):
    """Regression: Referrer-Policy: no-referrer made browsers send
    'Origin: null' on form posts, and the CSRF check blocked every button.
    The policy must let the browser identify the site to itself."""
    r = client().get("/landing")
    assert r.headers["referrer-policy"] in ("same-origin", "strict-origin", "strict-origin-when-cross-origin")
    # a same-site browser post (with its Origin header) goes through
    ok = client().post("/demo", headers={"Origin": BASE}, follow_redirects=False)
    assert ok.status_code == 303
    # while "null" and foreign origins stay blocked
    assert client().post("/demo", headers={"Origin": "null"}).status_code == 403
    assert client().post("/demo", headers={"Origin": "https://evil.example"}).status_code == 403
