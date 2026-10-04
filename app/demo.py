"""
"View demo": a throwaway user filled with made-up but realistic data, so
a visitor sees the whole app without signing up or getting Plaid keys.

Each visitor gets their own demo user (so clicking around never affects
anyone else); demo users older than DEMO_TTL are deleted whenever a new
demo starts. All names and amounts are invented -- nothing here comes from
a real bank account.
"""
import random
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    Category,
    ImportCapture,
    ReimbursementStatus,
    Transaction,
    TransactionType,
    User,
)

DEMO_TTL = timedelta(hours=24)
MONTHS_OF_HISTORY = 4  # the current month plus the 3 before it

E, I, T = TransactionType.EXPENSE, TransactionType.INCOME, TransactionType.TRANSFER


def _money(low: float, high: float) -> Decimal:
    return Decimal(str(round(random.uniform(low, high), 2)))


def delete_stale_demo_users(db: Session) -> int:
    """Removes demo users created more than DEMO_TTL ago, with everything
    they own. Returns how many were removed."""
    cutoff = datetime.utcnow() - DEMO_TTL
    stale = db.scalars(select(User.id).where(User.is_demo == True, User.created_at < cutoff)).all()  # noqa: E712
    return delete_demo_users(db, stale)


def delete_demo_users(db: Session, user_ids) -> int:
    """Deletes these demo users and everything they own. Refuses to touch
    anyone who isn't a demo user."""
    stale = db.scalars(select(User.id).where(User.id.in_(list(user_ids)), User.is_demo == True)).all()  # noqa: E712
    if not stale:
        return 0
    account_ids = select(Account.id).where(Account.user_id.in_(stale))
    db.execute(delete(Transaction).where(
        or_(Transaction.account_id.in_(account_ids), Transaction.to_account_id.in_(account_ids))
    ))
    db.execute(delete(ImportCapture).where(ImportCapture.account_id.in_(account_ids)))
    db.execute(delete(Account).where(Account.user_id.in_(stale)))
    db.execute(delete(User).where(User.id.in_(stale)))
    db.commit()
    return len(stale)


def create_demo_user(db: Session, today: date | None = None) -> User:
    today = today or date.today()
    start = date(today.year, today.month, 1) - relativedelta(months=MONTHS_OF_HISTORY - 1)
    opening_date = start - timedelta(days=1)

    user = User(
        email=f"demo-{secrets.token_hex(6)}@demo.clearbook",
        # Unusable credentials: there is no password that hashes to this.
        password_hash="!demo-" + secrets.token_hex(16),
        password_salt=secrets.token_hex(16),
        is_demo=True,
    )
    db.add(user)
    db.flush()

    checking = Account(user_id=user.id, name="Everyday Checking", type=AccountType.CHECKING,
                       opening_balance=Decimal("4850.00"), opening_balance_date=opening_date)
    card = Account(user_id=user.id, name="Rewards Card", type=AccountType.CREDIT_CARD,
                   opening_balance=Decimal("412.60"), opening_balance_date=opening_date)
    cash = Account(user_id=user.id, name="Cash", type=AccountType.CASH,
                   opening_balance=Decimal("120.00"), opening_balance_date=opening_date)
    db.add_all([checking, card, cash])
    db.flush()

    cat = {c.name: c.id for c in db.scalars(select(Category)).all()}
    rows: list[Transaction] = []

    def add(day: date, amount, type_, account, category=None, note=None, **extra):
        if day > today:
            return  # never show the future
        rows.append(Transaction(date=day, amount=Decimal(amount), type=type_, account_id=account.id,
                                category_id=cat.get(category) if category else None, note=note, **extra))

    card_spend_by_month: dict[date, Decimal] = {}

    def on_card(day, amount, category, note, **extra):
        if day <= today:
            key = date(day.year, day.month, 1)
            card_spend_by_month[key] = card_spend_by_month.get(key, Decimal(0)) + Decimal(amount)
        add(day, amount, E, card, category, note, **extra)

    trip_month = random.randrange(1, MONTHS_OF_HISTORY)      # one month with a trip
    laptop_month = random.randrange(0, MONTHS_OF_HISTORY)    # one big one-off purchase

    for m in range(MONTHS_OF_HISTORY):
        month = start + relativedelta(months=m)
        last_day = (month + relativedelta(months=1) - timedelta(days=1)).day
        day = lambda d: month.replace(day=min(d, last_day))  # noqa: E731

        # ---- income
        for payday in (1, 15):
            add(day(payday), _money(2080, 2190), I, checking, "Salary", "Acme Analytics payroll")
        if m == 1:
            add(day(19), "386.40", I, checking, "Reimbursement", "Work travel reimbursement",
                exclude_from_living=True)
        for _ in range(random.randint(1, 2)):  # friends paying their share back
            add(day(random.randint(3, 27)), _money(12, 45), I, checking, "Food",
                random.choice(["Zelle from Priya", "Venmo from Marco", "Zelle from Sam"]), is_refund=True)

        # ---- fixed costs
        add(day(1), "1650.00", E, checking, "Rent", "Rent - Maple Street Apartments")
        add(day(6), _money(58, 84), E, checking, "Other", "City Power & Gas")
        add(day(9), "45.00", E, checking, "Other", "FiberNet Internet")
        for d, amt, name in [(3, "15.49", "StreamFlix"), (11, "10.99", "Tunely Music"), (21, "2.99", "CloudBox Storage")]:
            on_card(day(d), amt, "Subscriptions", name)

        # ---- everyday spending
        for d in range(1, last_day + 1):
            dd = month.replace(day=d)
            if dd.weekday() < 5 and random.random() < 0.8:          # weekday commute
                on_card(dd, "2.90", "Transport", "City Transit")
                if random.random() < 0.85:
                    on_card(dd, "2.90", "Transport", "City Transit")
            if dd.weekday() < 5 and random.random() < 0.45:
                on_card(dd, _money(3.75, 6.50), "Food", random.choice(["Bean & Leaf Coffee", "Daily Grind"]))
            if dd.weekday() < 5 and random.random() < 0.3:
                on_card(dd, _money(9, 16), "Food", random.choice(["Campus Deli", "Green Bowl", "Noodle Bar"]))
        for week in range(4):
            on_card(day(2 + week * 7 + random.randint(0, 2)), _money(38, 92), "Groceries",
                    random.choice(["Corner Market", "FreshCo Grocery", "Corner Market"]))
        for _ in range(random.randint(2, 4)):
            on_card(day(random.randint(4, 28)), _money(28, 74), "Food",
                    random.choice(["Trattoria Roma", "Sakura Sushi", "Taco Republic", "The Burger Joint"]))
        if random.random() < 0.8:
            on_card(day(random.randint(5, 25)), _money(9, 38), "Health", "Main St Pharmacy")
        for _ in range(random.randint(1, 3)):
            on_card(day(random.randint(2, 28)), _money(18, 85), "Shopping",
                    random.choice(["Online Marketplace", "Threadline Apparel", "HomeGoods Plus"]))
        if random.random() < 0.6:
            on_card(day(random.randint(8, 26)), _money(14, 40), "Entertainment",
                    random.choice(["Cinema Paradiso", "Indie Games Store", "Bowling Lanes"]))
        add(day(2), "60.00", T, checking, None, "ATM withdrawal", to_account_id=cash.id)
        for _ in range(random.randint(2, 4)):  # cash: food carts, laundry
            add(day(random.randint(1, 28)), _money(4, 14), E, cash,
                random.choice(["Food", "Other"]), random.choice(["Food cart", "Laundromat", "Farmers market"]))

        # ---- the one-offs that make Living vs Total interesting
        if m == trip_month:
            on_card(day(10), _money(240, 320), "Travel", "SkyHigh Airlines")
            on_card(day(12), _money(180, 260), "Travel", "Harbor View Hotel")
            on_card(day(13), _money(22, 48), "Travel", "Rideshare")
        if m == laptop_month:
            on_card(day(17), "1199.00", "Shopping", "Laptop (one-off)", exclude_from_living=True)
        if m == 2:
            on_card(day(14), "64.20", "Shopping", "Gift for Mom", reimbursable=True,
                    reimbursement_status=ReimbursementStatus.PENDING)

    # ---- pay off each month's card statement on the 25th of the next month
    for month_start, spent in sorted(card_spend_by_month.items()):
        pay_day = (month_start + relativedelta(months=1)).replace(day=25)
        add(pay_day, spent.quantize(Decimal("0.01")), T, checking, None,
            "Rewards Card payment", to_account_id=card.id)

    db.add_all(rows)
    db.commit()
    return user
