"""
Business logic that isn't just CRUD -- balance calculation, monthly
summaries, and the reimbursement views we designed for.

Balances are computed on the fly from the transaction log rather than
stored as a running counter on Account. For a personal-scale dataset this
is fast enough, and it avoids an entire class of bugs where a stored
balance drifts out of sync after an edit or delete.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    Category,
    CategoryKind,
    ImportCapture,
    ReimbursementStatus,
    Transaction,
    TransactionType,
)

# Category colors for the stacked trend charts, derived from the app's
# "Dark Green Tropical" palette (navy / teal / green family, see style.css):
# the palette's own teal and green plus cool neighbors at usable
# lightness. Validated with the dataviz skill's validate_palette.js on the
# #FFFFFF chart surface -- all checks pass for *adjacent* slots, which is
# why the charts stack series in this fixed order. The lime and lavender
# are under 3:1 against white, so bars always carry hover tooltips and a
# labeled legend.
CATEGORY_COLOR_SLOTS = [
    "#0d5c91",  # deep blue (palette navy, lifted)
    "#2C9D90",  # palette teal
    "#5c39b5",  # indigo
    "#77b30e",  # lime
    "#0e78e4",  # bright blue
    "#1D8B65",  # palette green
    "#9e8bf7",  # lavender
    "#4a620b",  # olive -- the "Other" fold-bucket, below
]


def get_account_balance(db: Session, account: Account) -> Decimal:
    balance = account.opening_balance

    # opening_balance is a snapshot as of opening_balance_date -- anything
    # already reflected in that snapshot must not also be summed here, or
    # a transaction dated before it (e.g. imported statement history that
    # predates when the account was set up in the app) double-counts.
    outgoing = db.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.date >= account.opening_balance_date,
        )
    ).all()
    incoming = db.scalars(
        select(Transaction).where(
            Transaction.to_account_id == account.id,
            Transaction.date >= account.opening_balance_date,
        )
    ).all()

    is_liability = account.type == AccountType.CREDIT_CARD

    for t in outgoing:
        if t.type == TransactionType.INCOME:
            # A credit/refund posted directly to a card (cashback, a
            # merchant credit) reduces what you owe, same direction as an
            # incoming transfer below -- it doesn't add to it.
            balance += -t.amount if is_liability else t.amount
        elif t.type == TransactionType.EXPENSE:
            # On a credit card, spending increases what you owe.
            # On cash/checking, spending decreases what you have.
            balance += t.amount if is_liability else -t.amount
        elif t.type == TransactionType.TRANSFER:
            # Money leaving this account.
            balance += t.amount if is_liability else -t.amount

    for t in incoming:
        # Only TRANSFER transactions have a to_account.
        # Paying down a credit card reduces what's owed; landing in an
        # asset account increases what you have.
        balance += -t.amount if is_liability else t.amount

    return balance


def get_all_balances(db: Session) -> list[tuple[Account, Decimal]]:
    accounts = db.scalars(select(Account).order_by(Account.id)).all()
    return [(a, get_account_balance(db, a)) for a in accounts]


def get_total_balance(balances: list[tuple[Account, Decimal]]) -> Decimal:
    """Net total across accounts. Credit card balances are what you owe
    (a liability), so they subtract rather than add -- otherwise carrying
    card debt would make your total look bigger, not smaller."""
    total = Decimal(0)
    for account, balance in balances:
        total += -balance if account.type == AccountType.CREDIT_CARD else balance
    return total


def spend_amount(t: Transaction) -> Decimal:
    """What t adds to spending: an expense counts in full, a refund or
    payback subtracts, everything else is zero."""
    if t.type == TransactionType.EXPENSE:
        return t.amount
    if t.type == TransactionType.INCOME and t.is_refund:
        return -t.amount
    return Decimal(0)


def earned_amount(t: Transaction) -> Decimal:
    """What t adds to income -- refunds and paybacks don't count."""
    return t.amount if t.type == TransactionType.INCOME and not t.is_refund else Decimal(0)


def get_month_summary(
    db: Session, year: int, month: int, account_id: int | None = None
) -> dict:
    start = date(year, month, 1)
    end = start + relativedelta(months=1)

    query = select(Transaction).where(Transaction.date >= start, Transaction.date < end)
    if account_id is not None:
        # Matches either side so a transfer shows up whichever account
        # you're looking at -- e.g. paying down a card shows as an
        # outflow on checking and an inflow on the card.
        query = query.where(
            or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id)
        )
    txns = db.scalars(query).all()

    income = sum((earned_amount(t) for t in txns), Decimal(0))
    expenses = sum((spend_amount(t) for t in txns), Decimal(0))
    # Same as `expenses`/`income` but skips anything flagged
    # exclude_from_living (tuition, a security deposit refund, ...) --
    # shown alongside the real total, not instead of it, so a big one-off
    # doesn't drown out day-to-day spend without also hiding that it
    # happened. Applies to income too -- a deposit refund inflates
    # "living net" the same way an unflagged tuition payment deflates it.
    living_expenses = sum(
        (spend_amount(t) for t in txns if not t.exclude_from_living), Decimal(0)
    )
    living_income = sum(
        (earned_amount(t) for t in txns if not t.exclude_from_living), Decimal(0)
    )

    by_category: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    by_category_living: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for t in txns:
        spent = spend_amount(t)
        if spent and t.category:
            by_category[t.category.name] += spent
            if not t.exclude_from_living:
                by_category_living[t.category.name] += spent
    # A category a refund more than cancelled out this month isn't spending.
    by_category = {k: v for k, v in by_category.items() if v > 0}
    by_category_living = {k: v for k, v in by_category_living.items() if v > 0}

    return {
        "start": start,
        "income": income,
        "expenses": expenses,
        "living_expenses": living_expenses,
        "living_income": living_income,
        "net": income - expenses,
        "living_net": living_income - living_expenses,
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "by_category_living": dict(sorted(by_category_living.items(), key=lambda kv: -kv[1])),
        "transactions": sorted(txns, key=lambda t: t.date, reverse=True),
    }


def get_category_color_series(db: Session, kind: CategoryKind = CategoryKind.EXPENSE) -> list[dict]:
    """Fixed {label, color, category_ids} assignment for the stacked trend
    charts: the first 7 categories of `kind` (by id, i.e. creation order)
    get a dedicated hue from CATEGORY_COLOR_SLOTS; every other category of
    that kind folds into a shared "Other" bucket on the 8th. Fixed by
    category id, never by how much each spent, so a category's color never
    changes when the visible time range does."""
    categories = db.scalars(select(Category).where(Category.kind == kind).order_by(Category.id)).all()

    series = [
        {"label": c.name, "color": CATEGORY_COLOR_SLOTS[i], "category_ids": {c.id}}
        for i, c in enumerate(categories[:7])
    ]
    if len(categories) > 7:
        series.append(
            {
                "label": "Other",
                "color": CATEGORY_COLOR_SLOTS[7],
                "category_ids": {c.id for c in categories[7:]},
            }
        )
    return series


def get_monthly_category_trend(
    db: Session,
    start: date,
    months: int,
    kind: CategoryKind = CategoryKind.EXPENSE,
    living_only: bool = False,
    category_id: int | None = None,
) -> dict:
    """Per-month totals broken out by category, shaped for the stacked bar
    charts on Trends: {months: [label, ...], series: [{label, color,
    values: [float per month]}]}. Series stay in the fixed legend order and
    the chart stacks them in that same order every month -- the
    categorical palette is only validated for *that* adjacency, so sorting
    a month's stack by size would put untested color pairs side by side.
    Only series with money somewhere in the range are returned.

    `category_id` narrows to a single category (the "Highlight category"
    filter). Uncategorized transactions count toward "Other" -- otherwise a
    month's bar would silently come up short of the Dashboard's total."""
    end = start + relativedelta(months=months)
    spending = kind == CategoryKind.EXPENSE
    series = get_category_color_series(db, kind)
    index_by_category_id = {cid: idx for idx, s in enumerate(series) for cid in s["category_ids"]}
    other_idx = next((i for i, s in enumerate(series) if s["label"] in ("Other", "Other Income")), None)

    # Spending = expenses minus refunds/paybacks (in their category);
    # income = income that isn't a refund.
    query = select(Transaction).where(
        Transaction.date >= start,
        Transaction.date < end,
        Transaction.type.in_([TransactionType.EXPENSE, TransactionType.INCOME]),
    )
    if living_only:
        query = query.where(Transaction.exclude_from_living == False)  # noqa: E712
    if category_id is not None:
        query = query.where(Transaction.category_id == category_id)

    month_starts = [start + relativedelta(months=i) for i in range(months)]
    position = {(m.year, m.month): i for i, m in enumerate(month_starts)}
    values = [[Decimal(0)] * months for _ in series]
    for t in db.scalars(query):
        amount = spend_amount(t) if spending else earned_amount(t)
        if not amount:
            continue
        idx = index_by_category_id.get(t.category_id, other_idx)
        col = position.get((t.date.year, t.date.month))
        if idx is not None and col is not None:
            values[idx][col] += amount
    # A stacked bar can't show a category a refund more than cancelled
    # out in some month; floor it at zero (rare, and tiny when it happens).
    values = [[max(v, Decimal(0)) for v in row] for row in values]

    return {
        "months": [m.strftime("%b %Y") for m in month_starts],
        "series": [
            {"label": s["label"], "color": s["color"], "values": [float(v) for v in values[i]]}
            for i, s in enumerate(series)
            if any(values[i])
        ],
    }


MAX_BALANCE_POINTS = 120


def _balance_history_boundaries(start: date, end: date, granularity: str) -> list[date]:
    """One date per point on the balance-over-time chart -- the *last* day
    included in that point's bucket (a month's boundary is its last day,
    not its first, so "Aug 2026" means the balance as of Aug 31). Capped
    at MAX_BALANCE_POINTS by keeping only the most recent points -- a
    multi-year daily range would otherwise render hundreds of unreadable
    points."""
    end_of_range = (end + relativedelta(months=1)) - timedelta(days=1)

    if granularity == "month":
        months = (end.year - start.year) * 12 + (end.month - start.month) + 1
        boundaries = [
            (start + relativedelta(months=i + 1)) - timedelta(days=1) for i in range(months)
        ]
    elif granularity == "week":
        boundaries = []
        d = start
        while d <= end_of_range:
            boundaries.append(d)
            d += timedelta(days=7)
        if not boundaries or boundaries[-1] != end_of_range:
            boundaries.append(end_of_range)
    else:  # day
        boundaries = []
        d = start
        while d <= end_of_range:
            boundaries.append(d)
            d += timedelta(days=1)

    return boundaries[-MAX_BALANCE_POINTS:]


def get_balance_history(
    db: Session, start: date, end: date, granularity: str = "month"
) -> list[dict]:
    """Total balance (across every account, credit cards as liabilities --
    same convention as get_total_balance) sampled at each period boundary
    in [start, end]. Walks the transaction log once in date order,
    applying each transaction's effect to a running per-account balance
    and sampling the total whenever a boundary is crossed, rather than
    recomputing get_account_balance from scratch at every point. Fetches
    every transaction up to the cutoff (not just ones after `start`) so
    the running balance is correct even when the chart's visible range is
    capped -- see _balance_history_boundaries."""
    accounts = db.scalars(select(Account)).all()
    balance = {a.id: a.opening_balance for a in accounts}
    is_liability = {a.id: a.type == AccountType.CREDIT_CARD for a in accounts}
    # A transaction dated before an account's own opening_balance_date is
    # already reflected in that snapshot -- applying its effect too would
    # double-count it (same rule as get_account_balance).
    opening_balance_date = {a.id: a.opening_balance_date for a in accounts}

    boundaries = _balance_history_boundaries(start, end, granularity)
    if not boundaries:
        return []

    txns = db.scalars(
        select(Transaction).where(Transaction.date <= boundaries[-1]).order_by(Transaction.date)
    ).all()

    def total_balance() -> Decimal:
        total = Decimal(0)
        for account_id, b in balance.items():
            total += -b if is_liability[account_id] else b
        return total

    points = []
    txn_idx = 0
    for boundary in boundaries:
        while txn_idx < len(txns) and txns[txn_idx].date <= boundary:
            t = txns[txn_idx]
            from_predates_opening = t.date < opening_balance_date.get(t.account_id, t.date)
            if t.type == TransactionType.INCOME and not from_predates_opening:
                balance[t.account_id] += -t.amount if is_liability[t.account_id] else t.amount
            elif t.type == TransactionType.EXPENSE and not from_predates_opening:
                balance[t.account_id] += t.amount if is_liability[t.account_id] else -t.amount
            elif t.type == TransactionType.TRANSFER:
                if not from_predates_opening:
                    balance[t.account_id] += t.amount if is_liability[t.account_id] else -t.amount
                to_predates_opening = t.to_account_id is not None and t.date < opening_balance_date.get(
                    t.to_account_id, t.date
                )
                if t.to_account_id is not None and not to_predates_opening:
                    balance[t.to_account_id] += (
                        -t.amount if is_liability[t.to_account_id] else t.amount
                    )
            txn_idx += 1
        points.append({"date": boundary, "balance": total_balance()})

    return points


def _trailing_average(
    db: Session,
    txn_type: TransactionType,
    months: int,
    account_id: int | None,
    living_only: bool,
    as_of: date | None = None,
) -> tuple[Decimal, int]:
    """Shared windowing logic behind get_trailing_average_expense/income:
    divides by however many months of matching history actually exist
    (capped at `months`), not always by `months` -- otherwise a fresh
    ledger with a few days of data would understate the average by 10-20x
    until a full window of history accumulates. Returns (average, months
    the average is actually based on) so the UI can label it honestly.

    as_of anchors the trailing window to a specific month instead of
    always the current one -- the dashboard passes whichever month is
    being viewed, so browsing to a past month shows the "typical" figure
    as it stood then, not one quietly computed through today (which
    would leak months the viewed period hasn't reached yet)."""
    as_of_month = date((as_of or date.today()).year, (as_of or date.today()).month, 1)
    # Exclusive upper bound -- nothing dated after the viewed month counts,
    # same reasoning as the lower bound below.
    window_end = as_of_month + relativedelta(months=1)
    # "Trailing `months`" = the viewed (partial, if current) month plus the
    # (months - 1) months before it, so the window spans exactly `months`
    # calendar-month buckets -- keeps the numerator (summed months) and
    # denominator (months_covered below) counting the same thing.
    window_start = as_of_month - relativedelta(months=months - 1)

    # Spending averages net out refunds/paybacks; income averages skip them.
    types = (
        [TransactionType.EXPENSE, TransactionType.INCOME]
        if txn_type == TransactionType.EXPENSE
        else [TransactionType.INCOME]
    )
    amount_of = spend_amount if txn_type == TransactionType.EXPENSE else earned_amount

    def _scope(query):
        query = query.where(Transaction.type.in_(types), Transaction.date < window_end)
        if account_id is not None:
            query = query.where(Transaction.account_id == account_id)
        if living_only:
            query = query.where(Transaction.exclude_from_living == False)  # noqa: E712
        return query

    # History starts at the first real expense (or income) -- a refund
    # dated earlier shouldn't stretch the averaging window.
    earliest = db.scalar(
        _scope(select(func.min(Transaction.date))).where(Transaction.type == txn_type)
    )
    if earliest is None:
        return Decimal(0), 0

    start = max(window_start, date(earliest.year, earliest.month, 1))

    txns = db.scalars(_scope(select(Transaction)).where(Transaction.date >= start)).all()
    total = sum((amount_of(t) for t in txns), Decimal(0))

    months_covered = (as_of_month.year - start.year) * 12 + (as_of_month.month - start.month) + 1
    months_covered = max(1, min(months, months_covered))

    return total / months_covered, months_covered


def get_trailing_average_expense(
    db: Session,
    months: int = 12,
    account_id: int | None = None,
    living_only: bool = False,
    as_of: date | None = None,
) -> tuple[Decimal, int]:
    """Smoothed monthly spend, so a single lumpy cost (tuition, etc.)
    doesn't make one month look catastrophic and the rest artificially
    frugal -- shown alongside the raw monthly total, not instead of it.
    living_only=True excludes anything flagged exclude_from_living, for
    the "typical living expense" figure. as_of anchors the trailing
    window to a specific month (defaults to the current one)."""
    return _trailing_average(db, TransactionType.EXPENSE, months, account_id, living_only, as_of)


def get_trailing_average_income(
    db: Session,
    months: int = 12,
    account_id: int | None = None,
    living_only: bool = False,
    as_of: date | None = None,
) -> tuple[Decimal, int]:
    """Same idea as get_trailing_average_expense but for income --
    living_only=True excludes anything flagged exclude_from_living (a
    deposit refund, a one-off reimbursement, ...), for the "typical
    living income" figure."""
    return _trailing_average(db, TransactionType.INCOME, months, account_id, living_only, as_of)


def get_pending_reimbursements(db: Session, account_id: int | None = None) -> list[Transaction]:
    query = select(Transaction).where(
        Transaction.reimbursable == True,  # noqa: E712
        Transaction.reimbursement_status == ReimbursementStatus.PENDING,
    )
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    return db.scalars(query).all()


def log_import_capture(
    db: Session,
    account_id: int,
    transactions_found: int,
    bank_balance: Decimal | None = None,
    app_balance: Decimal | None = None,
    balance_matches: bool | None = None,
) -> ImportCapture:
    capture = ImportCapture(
        account_id=account_id,
        transactions_found=transactions_found,
        bank_balance=bank_balance,
        app_balance=app_balance,
        balance_matches=balance_matches,
    )
    db.add(capture)
    db.commit()
    return capture


DEFAULT_EXPENSE_CATEGORIES = [
    "Food",
    "Groceries",
    "Rent",
    "Transport",
    "Subscriptions",
    "Education",
    "Health",
    "Shopping",
    "Entertainment",
    "Travel",
    "Other",
]
DEFAULT_INCOME_CATEGORIES = [
    "Salary",
    "Freelance",
    "Reimbursement",
    "Interest",
    "Gift",
    "Other Income",
]


def seed_default_categories(db: Session) -> None:
    existing = {c.name for c in db.scalars(select(Category)).all()}
    for name in DEFAULT_EXPENSE_CATEGORIES:
        if name not in existing:
            db.add(Category(name=name, kind="expense"))
    for name in DEFAULT_INCOME_CATEGORIES:
        if name not in existing:
            db.add(Category(name=name, kind="income"))
    db.commit()


def relative_time(dt: datetime) -> str:
    seconds = (datetime.utcnow() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''} ago"
