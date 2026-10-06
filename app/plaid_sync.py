"""
Posts Plaid transactions straight into the ledger -- no review queue.

What stands in for a human reviewer:
- Plaid's transaction id is stored on the row (unique), so a re-sync can
  never post the same bank transaction twice.
- Before creating anything, an entry you already logged by hand (same
  account, amount and direction, within MATCH_WINDOW_DAYS) is *adopted* --
  linked to the Plaid id instead of duplicated.
- On a newly linked account's first sync, anything dated before your
  last entry for it is adopt-only: that stretch is already reconciled, so
  unmatched history is skipped rather than risk double-counting.
- A card payment seen by both checking and the card becomes one transfer.
- A pending transaction is updated in place when it posts, so edits made
  to it (category, note) survive.
- After every sync the app's balance is compared with the bank's; a
  mismatch is shown on the Dashboard and Accounts page.
"""
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Account,
    AccountType,
    Category,
    CategoryKind,
    ImportCapture,
    Transaction,
    TransactionType,
)
from app.plaid_client import PlaidCreds, describe_error, get_balance, sync_transactions
from app.services import get_account_balance, log_import_capture, owned_account_ids
from app.token_crypto import decrypt_token

# Matching a Plaid transaction to one you logged by hand. Banks post
# *after* you buy -- never before -- and BofA card swipes (e.g. MTA fares)
# were observed posting up to 5 days late, with no purchase date sent to
# Plaid. So look well back, barely forward.
POSTING_LAG_DAYS = 5
POSTING_LEAD_DAYS = 1
PAIR_WINDOW_DAYS = 5
CARD_PAYMENT = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
TRANSFER_PRIMARIES = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"}

# Plaid personal-finance category -> this app's seeded category names.
# Detailed codes take precedence over primary ones.
PLAID_CATEGORY_MAP = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "FOOD_AND_DRINK": "Food",
    "TRANSPORTATION": "Transport",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Rent",
    "MEDICAL": "Health",
    "PERSONAL_CARE": "Health",
    "GENERAL_MERCHANDISE": "Shopping",
    "ENTERTAINMENT": "Entertainment",
    "INCOME_WAGES": "Salary",
    "INCOME_INTEREST_EARNED": "Interest",
}

# account_id -> readable error from its most recent failed sync. Shown on
# the Accounts and Review pages, because an auto-sync that fails silently
# (e.g. Plaid needing a bank re-login) would just quietly stop importing.
# In-memory is enough: every app launch re-syncs and rebuilds it.
LAST_SYNC_ERRORS: dict[int, str] = {}

_SYNC_LOCK = threading.Lock()

# Plaid refreshes bank data a few times a day, so re-syncing more often
# than this just repeats the same answer.
SYNC_INTERVAL_HOURS = 4

# A balance gap that lasts longer than this isn't a charge still settling.
DRIFT_GRACE_DAYS = 3

# Plaid errors only you can fix (by signing in to the bank again via
# Link). Anything else -- network, rate limits, Plaid hiccups -- is
# retried by the next periodic sync without bothering you.
NEEDS_USER_ERRORS = {
    "ITEM_LOGIN_REQUIRED",
    "PENDING_EXPIRATION",
    "PENDING_DISCONNECT",
    "INVALID_CREDENTIALS",
    "INVALID_MFA",
    "ITEM_LOCKED",
    "USER_SETUP_REQUIRED",
    "ACCESS_NOT_GRANTED",
    "NO_ACCOUNTS",
}


# An account created by "Connect a bank" starts with this placeholder
# opening date, meaning "history not in yet": every synced transaction
# counts, and once the first ones arrive the opening balance is
# back-computed from the bank's balance (settle_opening_balance).
HISTORY_PENDING = date(2000, 1, 1)


def settle_opening_balance(db: Session, account: Account, bank_balance: Decimal) -> None:
    """For a bank-connected account: choose the opening balance so the
    app's posted balance equals the bank's, and date it at the earliest
    synced transaction -- so the full history shows *and* the balance is
    right. Until transactions arrive, the balance is just the bank's."""
    activity = _posted_balance(db, account) - account.opening_balance
    account.opening_balance = bank_balance - activity
    earliest = db.scalar(
        select(func.min(Transaction.date)).where(
            or_(Transaction.account_id == account.id, Transaction.to_account_id == account.id)
        )
    )
    if earliest is not None:
        account.opening_balance_date = earliest
    db.commit()


class NoPlaidKeys(RuntimeError):
    """The user hasn't saved their Plaid keys yet."""


def creds_for(user) -> PlaidCreds:
    """The user's own Plaid keys, decrypted for one call. Raises
    NoPlaidKeys if they haven't added any."""
    if not user.plaid_client_id or not user.plaid_secret:
        raise NoPlaidKeys("Add your Plaid keys first.")
    return PlaidCreds(user.plaid_client_id, decrypt_token(user.plaid_secret))


def _within(days: int, when):
    return (Transaction.date >= when - timedelta(days=days)) & (
        Transaction.date <= when + timedelta(days=days)
    )


def _posted_after(when):
    """A hand-entered row this Plaid transaction could be the posting of."""
    return (Transaction.date >= when - timedelta(days=POSTING_LAG_DAYS)) & (
        Transaction.date <= when + timedelta(days=POSTING_LEAD_DAYS)
    )


def _is_transferish(t: dict) -> bool:
    return t["category_detailed"] == CARD_PAYMENT or t["category_primary"] in TRANSFER_PRIMARIES


def _pick_category(db: Session, account: Account, t: dict, txn_type: TransactionType) -> int | None:
    # Your own past choice for this merchant wins over Plaid's guess --
    # yours only, never another user's.
    learned = db.scalar(
        select(Transaction.category_id)
        .where(Transaction.note == t["merchant"], Transaction.category_id.isnot(None),
               Transaction.type == txn_type,
               Transaction.account_id.in_(owned_account_ids(account.user_id)))
        .order_by(Transaction.date.desc())
        .limit(1)
    )
    if learned:
        return learned
    name = PLAID_CATEGORY_MAP.get(t["category_detailed"]) or PLAID_CATEGORY_MAP.get(
        t["category_primary"]
    )
    if txn_type == TransactionType.INCOME and name not in ("Salary", "Interest"):
        name = "Other Income"
    elif txn_type == TransactionType.EXPENSE and name is None:
        name = "Other"
    return db.scalar(select(Category.id).where(Category.name == name)) if name else None


# Accounts that only ever move money between people -- anything arriving
# there is someone paying you back, not pay.
P2P_WALLET_NAMES = ("venmo", "cash app", "paypal")


def _is_refund(account: Account, t: dict) -> bool:
    """Incoming money that should reduce spending rather than count as
    income: a card credit (store refund, statement credit, offer), or a
    person paying you back over Zelle/Venmo. Pay and interest never are."""
    if t["outflow"] or t["category_primary"] == "INCOME":
        return False
    if account.type == AccountType.CREDIT_CARD:
        return True
    if any(w in account.name.lower() for w in P2P_WALLET_NAMES):
        return True
    return t["merchant"].lower().startswith("zelle") or t["category_primary"] == "TRANSFER_IN"


def _refund_category(db: Session, account: Account, t: dict) -> int | None:
    """The expense category a refund comes off: whatever you last filed
    this merchant's spending under (an Uber refund -> Transport), else
    Plaid's guess. A payback from a friend has neither, so it stays
    uncategorized (it still comes off total spending)."""
    learned = db.scalar(
        select(Transaction.category_id)
        .where(Transaction.note == t["merchant"], Transaction.category_id.isnot(None),
               Transaction.type == TransactionType.EXPENSE,
               Transaction.account_id.in_(owned_account_ids(account.user_id)))
        .order_by(Transaction.date.desc())
        .limit(1)
    )
    if learned:
        return learned
    name = PLAID_CATEGORY_MAP.get(t["category_detailed"]) or PLAID_CATEGORY_MAP.get(
        t["category_primary"]
    )
    if name is None or name in ("Salary", "Interest"):
        return None
    return db.scalar(select(Category.id).where(Category.name == name, Category.kind == CategoryKind.EXPENSE))


def _find_by_plaid_id(db: Session, account: Account, plaid_id: str) -> Transaction | None:
    # Only among this user's own transactions (security review: Plaid's
    # ids are unique, but nothing here should ever reach another user's rows).
    return db.scalar(
        select(Transaction).where(
            Transaction.account_id.in_(owned_account_ids(account.user_id)),
            or_(
                Transaction.plaid_transaction_id == plaid_id,
                Transaction.plaid_pair_transaction_id == plaid_id,
            ),
        )
    )


def _adopt(db: Session, account: Account, t: dict) -> bool:
    """Links t to a hand-entered row it duplicates, if there is one."""
    if t["outflow"]:
        candidates = select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.type.in_([TransactionType.EXPENSE, TransactionType.TRANSFER]),
            Transaction.plaid_transaction_id.is_(None),
        )
    else:
        candidates = select(Transaction).where(
            or_(
                (Transaction.account_id == account.id)
                & (Transaction.type == TransactionType.INCOME)
                & Transaction.plaid_transaction_id.is_(None),
                # The receiving side of a transfer you logged from another
                # account (e.g. a card payment logged on checking).
                (Transaction.to_account_id == account.id)
                & (Transaction.type == TransactionType.TRANSFER)
                & Transaction.plaid_pair_transaction_id.is_(None),
            )
        )
    rows = db.scalars(
        candidates.where(Transaction.amount == t["amount"], _posted_after(t["date"]))
    ).all()
    if not rows:
        return False
    # Oldest candidate, not nearest: banks post in FIFO order, and MTA
    # batches several days of fares into one posting date. Nearest-date
    # lets a batch grab the newest rides and strands the oldest ones
    # outside the next batch's window, so they get posted twice. With
    # oldest-first processing (see sync_plaid_account), oldest-first
    # claiming pairs identical amounts off in order.
    row = min(rows, key=lambda r: (r.date, r.id))
    if row.to_account_id == account.id and row.account_id != account.id:
        row.plaid_pair_transaction_id = t["plaid_id"]
    else:
        row.plaid_transaction_id = t["plaid_id"]
    row.pending = t["pending"]
    return True


def _pair_transfer(db: Session, account: Account, t: dict) -> bool:
    """Merges t with the other side of the same transfer, if that side has
    already been posted from another linked account."""
    if t["outflow"]:
        # A card charge is never the paying side of a transfer, even when
        # Plaid calls it a loan payment (a $10 campus ID-office charge got
        # merged with an unrelated $10 Zelle into checking that way).
        if account.type == AccountType.CREDIT_CARD:
            return False
        # The card saw the payment first and it was posted as income.
        row = db.scalar(
            select(Transaction).join(Account, Transaction.account_id == Account.id).where(
                Account.user_id == account.user_id,
                Account.type == AccountType.CREDIT_CARD,
                Transaction.account_id != account.id,
                Transaction.type == TransactionType.INCOME,
                Transaction.plaid_transaction_id.isnot(None),
                Transaction.plaid_pair_transaction_id.is_(None),
                Transaction.amount == t["amount"],
                _within(PAIR_WINDOW_DAYS, t["date"]),
            )
        )
        if row is None:
            return False
        row.plaid_pair_transaction_id = row.plaid_transaction_id
        row.plaid_transaction_id = t["plaid_id"]
        row.to_account_id, row.account_id = row.account_id, account.id
        row.type, row.category_id = TransactionType.TRANSFER, None
        return True
    # This is the receiving side; the paying side is a transfer still
    # waiting for its destination.
    row = db.scalar(
        select(Transaction).where(
            Transaction.account_id.in_(owned_account_ids(account.user_id)),
            Transaction.account_id != account.id,
            Transaction.type == TransactionType.TRANSFER,
            Transaction.to_account_id.is_(None),
            Transaction.plaid_transaction_id.isnot(None),
            Transaction.amount == t["amount"],
            _within(PAIR_WINDOW_DAYS, t["date"]),
        )
    )
    if row is None:
        return False
    row.to_account_id = account.id
    row.plaid_pair_transaction_id = t["plaid_id"]
    return True


def _funded_wallet(db: Session, account: Account, t: dict) -> Account | None:
    """The other Plaid-linked, non-card account t's merchant names (e.g. a
    BofA debit described "Venmo" while Venmo is linked), if any. Only a
    linked wallet counts: an unlinked one's payments aren't in the ledger,
    so the debit itself has to stay the expense."""
    if not t["outflow"] or account.type == AccountType.CREDIT_CARD:
        return None
    merchant = t["merchant"].lower()
    for other in db.scalars(
        select(Account).where(
            Account.user_id == account.user_id,
            Account.id != account.id,
            Account.plaid_access_token.isnot(None),
            Account.type != AccountType.CREDIT_CARD,
        )
    ):
        if len(other.name) >= 4 and other.name.lower() in merchant:
            return other
    return None


def _apply_added(db: Session, account: Account, t: dict, adopt_only: bool) -> bool:
    """Returns True if a new ledger row was created."""
    if _find_by_plaid_id(db, account, t["plaid_id"]):
        return False  # already posted -- re-syncs are harmless

    if t["pending_plaid_id"]:
        row = _find_by_plaid_id(db, account, t["pending_plaid_id"])
        if row:  # a pending charge just posted: update it in place
            if row.plaid_transaction_id == t["pending_plaid_id"]:
                row.plaid_transaction_id = t["plaid_id"]
            else:
                row.plaid_pair_transaction_id = t["plaid_id"]
            row.amount, row.date, row.pending = t["amount"], t["date"], t["pending"]
            return False

    if _adopt(db, account, t) or adopt_only:
        return False
    if _is_transferish(t) and _pair_transfer(db, account, t):
        return False

    is_card = account.type == AccountType.CREDIT_CARD
    wallet = _funded_wallet(db, account, t)
    to_account_id = None
    if not t["outflow"]:
        txn_type = TransactionType.INCOME
    elif wallet is not None:
        # A bank debit that tops up a wallet like Venmo: when a Venmo
        # payment exceeds the Venmo balance, Venmo pulls the difference
        # from the bank, but its own feed only shows the payment. Log the
        # pull as a transfer into the wallet, so the payment (an expense
        # on the wallet's side) is counted once and both balances match.
        txn_type, to_account_id = TransactionType.TRANSFER, wallet.id
    elif t["category_detailed"] == CARD_PAYMENT and not is_card:
        # Paying a card: a transfer, not spending. The destination gets
        # filled in when the card's side of the payment syncs.
        txn_type = TransactionType.TRANSFER
    else:
        txn_type = TransactionType.EXPENSE

    is_refund = txn_type == TransactionType.INCOME and _is_refund(account, t)
    if txn_type == TransactionType.TRANSFER:
        category_id = None
    elif is_refund:
        category_id = _refund_category(db, account, t)
    else:
        category_id = _pick_category(db, account, t, txn_type)

    db.add(
        Transaction(
            date=t["date"],
            amount=t["amount"],
            type=txn_type,
            account_id=account.id,
            to_account_id=to_account_id,
            category_id=category_id,
            note=t["merchant"],
            is_refund=is_refund,
            plaid_transaction_id=t["plaid_id"],
            pending=t["pending"],
        )
    )
    return True


def _apply_modified(db: Session, account: Account, t: dict) -> None:
    row = db.scalar(
        select(Transaction).where(
            Transaction.account_id.in_(owned_account_ids(account.user_id)),
            Transaction.plaid_transaction_id == t["plaid_id"],
        )
    )
    if row:
        row.amount, row.date, row.pending = t["amount"], t["date"], t["pending"]


def _pending_effects(db: Session, account: Account) -> list[Decimal]:
    """How much each pending row moves this account's balance, exactly as
    get_account_balance applied it."""
    is_liability = account.type == AccountType.CREDIT_CARD
    pending = db.scalars(
        select(Transaction).where(
            Transaction.pending == True,  # noqa: E712
            or_(Transaction.account_id == account.id, Transaction.to_account_id == account.id),
            Transaction.date >= account.opening_balance_date,
        )
    ).all()
    effects = []
    for t in pending:
        if t.to_account_id == account.id:
            effect = -t.amount if is_liability else t.amount
        elif t.type == TransactionType.INCOME:
            effect = -t.amount if is_liability else t.amount
        else:  # EXPENSE, or TRANSFER out of this account
            effect = t.amount if is_liability else -t.amount
        effects.append(effect)
    return effects


def _posted_balance(db: Session, account: Account) -> Decimal:
    """The app's balance counting only posted transactions -- comparable
    to the bank's posted ("current") balance, which excludes pending."""
    return get_account_balance(db, account) - sum(_pending_effects(db, account), Decimal(0))


def _explained_by_settling(gap: Decimal, pending_effects: list[Decimal]) -> bool:
    """True if the bank being ahead of Plaid's transaction feed explains
    the gap: the bank's balance already counts some charges as posted that
    the feed still lists as pending (seen with BofA MTA fares -- a $6 "mismatch"
    that was two $3 fares). That resolves itself on a later sync, so it
    isn't worth an alert."""
    target = round(gap * 100)
    reachable = {0}
    for effect in pending_effects[:30]:  # a subset-sum; keep it bounded
        cents = round(effect * 100)
        reachable |= {r + cents for r in reachable}
    return target in reachable


def _apply_removed(db: Session, account: Account, plaid_id: str) -> None:
    row = _find_by_plaid_id(db, account, plaid_id)
    if row is None:
        return
    if row.plaid_pair_transaction_id is None:
        db.delete(row)
    elif row.plaid_transaction_id == plaid_id:
        # Paying side vanished; keep the receiving side as a credit.
        row.plaid_transaction_id, row.plaid_pair_transaction_id = row.plaid_pair_transaction_id, None
        row.account_id, row.to_account_id = row.to_account_id, None
        row.type = TransactionType.INCOME
    else:
        # Receiving side vanished; the paying side waits for a destination.
        row.plaid_pair_transaction_id, row.to_account_id = None, None


def sync_plaid_account(db: Session, account: Account) -> int:
    """Returns how many new ledger rows were created. Everything -- the
    changes and the advanced cursor -- lands in one commit, so a failure
    part-way leaves nothing half-applied and the next sync retries it.
    Raises on any Plaid/network/decryption failure."""
    creds = creds_for(account.user)
    token = decrypt_token(account.plaid_access_token)
    first_sync = account.plaid_cursor is None
    result = sync_transactions(creds, token, account.plaid_account_id, account.plaid_cursor)

    backfill_until = None
    if first_sync:
        backfill_until = db.scalar(
            select(func.max(Transaction.date)).where(
                or_(Transaction.account_id == account.id, Transaction.to_account_id == account.id)
            )
        )

    created = 0
    # Posted before pending, then oldest first: anything already posted
    # happened before anything still pending, so it gets first claim on
    # hand-entered rows. Otherwise a pending $3 fare can grab the row an
    # older posted $3 fare belongs to, and that fare gets doubled.
    for t in sorted(result["added"], key=lambda t: (t["pending"], t["date"])):
        # Already baked into the opening-balance snapshot.
        if t["date"] < account.opening_balance_date:
            continue
        # Strictly before: the last hand-entered day may be only partly
        # entered (a $67.02 AMEX charge on it was skipped with <=). Anything
        # on that day you did enter is still adopted, not duplicated.
        adopt_only = backfill_until is not None and t["date"] < backfill_until
        created += _apply_added(db, account, t, adopt_only)
        db.flush()  # later matches in this batch must see earlier ones
    for t in result["modified"]:
        _apply_modified(db, account, t)
    for plaid_id in result["removed"]:
        _apply_removed(db, account, plaid_id)
        db.flush()

    account.plaid_cursor = result["next_cursor"]
    db.commit()

    bank_balance = get_balance(creds, token, account.plaid_account_id)
    if bank_balance is not None and account.opening_balance_date == HISTORY_PENDING:
        settle_opening_balance(db, account, bank_balance)
    app_balance = _posted_balance(db, account)
    matches = None
    if bank_balance is not None:
        matches = abs(app_balance - bank_balance) < Decimal("0.01") or _explained_by_settling(
            bank_balance - app_balance, _pending_effects(db, account)
        )
    log_import_capture(db, account.id, created, bank_balance, app_balance, matches)
    return created


def get_sync_alerts(db: Session, *, user_id: int) -> list[str]:
    """Only what *you* have to fix: a bank asking you to sign in again.
    The app's whole point is that syncing takes care of itself, so
    everything else stays off the Dashboard -- a network blip just retries
    on the next periodic sync, and a balance gap mid-settlement resolves
    itself (see get_balance_drift for the persistent kind)."""
    alerts = []
    # list() snapshots it -- the sync thread may be writing to it.
    for account_id, error in list(LAST_SYNC_ERRORS.items()):
        account = db.get(Account, account_id)
        if account and account.user_id == user_id and error.split(":", 1)[0] in NEEDS_USER_ERRORS:
            alerts.append(
                f"{account.name} needs you to sign in to your bank again -- "
                "Disconnect it on the Accounts page, then Connect it again."
            )
    return alerts


def get_balance_drift(db: Session, *, user_id: int) -> list[str]:
    """Linked accounts whose balance has disagreed with the bank on every
    sync for DRIFT_GRACE_DAYS -- long enough that it isn't a charge still
    settling, so a transaction really is missing or doubled. Shown quietly
    on the Accounts page only."""
    notes = []
    cutoff = datetime.utcnow() - timedelta(days=DRIFT_GRACE_DAYS)
    for account in db.scalars(
        select(Account).where(Account.user_id == user_id, Account.plaid_access_token.isnot(None))
    ):
        last = db.scalar(
            select(ImportCapture)
            .where(ImportCapture.account_id == account.id)
            .order_by(ImportCapture.id.desc())
            .limit(1)
        )
        if not last or last.balance_matches is not False:
            continue
        last_good = db.scalar(
            select(func.max(ImportCapture.created_at)).where(
                ImportCapture.account_id == account.id, ImportCapture.balance_matches == True  # noqa: E712
            )
        )
        if last_good is None or last_good < cutoff:
            gap = abs(last.app_balance - last.bank_balance)
            notes.append(
                f"{account.name} has been ${gap:,.2f} off from the bank for over "
                f"{DRIFT_GRACE_DAYS} days -- a transaction may be missing or doubled."
            )
    return notes


def get_last_synced(db: Session, *, user_id: int) -> datetime | None:
    """When bank data was last pulled for any of the user's linked accounts."""
    return db.scalar(
        select(func.max(ImportCapture.created_at))
        .join(Account, Account.id == ImportCapture.account_id)
        .where(Account.user_id == user_id, Account.plaid_access_token.isnot(None))
    )


def sync_account_recording_errors(db: Session, account: Account) -> str | None:
    """sync_plaid_account, but a failure is recorded (and returned)
    instead of raised -- one bank's problem shouldn't stop the others."""
    try:
        # One sync at a time: the periodic thread and a "Sync now" click
        # would otherwise both insert the same new transaction.
        with _SYNC_LOCK:
            sync_plaid_account(db, account)
    except Exception as e:  # SDK, network, or a token that won't decrypt
        db.rollback()
        LAST_SYNC_ERRORS[account.id] = describe_error(e)
        return LAST_SYNC_ERRORS[account.id]
    LAST_SYNC_ERRORS.pop(account.id, None)
    return None


def sync_all_linked_accounts(user_id: int | None = None) -> None:
    """Syncs every linked account (or just one user's). Uses its own
    session -- request sessions belong to request threads."""
    db = SessionLocal()
    try:
        query = select(Account).where(Account.plaid_access_token.isnot(None))
        if user_id is not None:
            query = query.where(Account.user_id == user_id)
        accounts = db.scalars(query).all()
        for account in accounts:
            error = sync_account_recording_errors(db, account)
            print(f"[plaid] {account.name}: {'FAILED -- ' + error if error else 'synced'}", flush=True)
    finally:
        db.close()


def sync_user_accounts(user_id: int) -> None:
    """Run after a user logs in, so their numbers are fresh."""
    sync_all_linked_accounts(user_id)


def sync_periodically() -> None:
    """Background loop started at app launch: sync now, then every
    SYNC_INTERVAL_HOURS for as long as the app keeps running."""
    while True:
        sync_all_linked_accounts()
        time.sleep(SYNC_INTERVAL_HOURS * 3600)
