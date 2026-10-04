"""
Data model.

This encodes the design decisions from planning:

- Credit cards are modeled as accounts with type CREDIT_CARD. A purchase on
  a card is an EXPENSE transaction against that account (increases what you
  owe). Paying the card bill is a TRANSFER from a checking/cash account into
  the card account (decreases what you owe). This is what avoids double
  counting and solves the "card charges settle after the month" problem --
  see get_account_balance() in services.py for how balances are derived.

- Lumpy, foreseeable costs (like semester tuition) aren't a special case --
  they're logged as a normal expense on the date paid. The 12-month
  trailing average in get_trailing_average_expense() is what keeps a
  single lumpy cost from making one month look catastrophic; the raw
  monthly total is deliberately left alone since it's accurate.

- Reimbursements are tracked with a lightweight flag + status on the
  original expense, rather than a full receivables ledger. When money
  actually arrives, it's logged as its own INCOME transaction.
"""
import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountType(str, enum.Enum):
    CASH = "cash"
    CHECKING = "checking"
    CREDIT_CARD = "credit_card"


class TransactionType(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class ReimbursementStatus(str, enum.Enum):
    PENDING = "pending"
    RECEIVED = "received"


class CategoryKind(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"


class User(Base):
    """One person using Clearbook. Passwords are never stored: only a
    salted scrypt hash (see app/auth.py). Everything else a user owns hangs
    off their accounts."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Always stored lowercased, so "A@x.com" and "a@x.com" are one account.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    password_salt: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # The user's own Plaid Sandbox keys (Clearbook has none of its own).
    # The secret is Fernet-encrypted (app/token_crypto.py) and only ever
    # decrypted in memory to make a Plaid call; never sent to the browser.
    # A throwaway "View demo" visitor (app/demo.py): generated data, no
    # usable password, deleted after DEMO_TTL. Can't set up Plaid.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    plaid_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plaid_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(back_populates="user")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Whose account this is. Transactions belong to accounts, so this one
    # column is what keeps every user's data separate (see
    # services.owned_account_ids).
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    user: Mapped["User"] = relationship(back_populates="accounts")
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[AccountType] = mapped_column(SAEnum(AccountType))
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    opening_balance_date: Mapped[date] = mapped_column(Date, default=date.today)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Set once the account is linked via Plaid (see app/plaid_client.py).
    # plaid_access_token is a long-lived credential -- it lives here, in
    # the gitignored db, same as every other real financial record.
    # plaid_account_id picks out this one account within a linked Item
    # (an Item can cover several), and plaid_cursor is the
    # /transactions/sync bookmark so each sync only fetches what's new.
    plaid_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    plaid_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plaid_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    transactions_from: Mapped[list["Transaction"]] = relationship(
        "Transaction", foreign_keys="Transaction.account_id", back_populates="account"
    )
    transactions_to: Mapped[list["Transaction"]] = relationship(
        "Transaction", foreign_keys="Transaction.to_account_id", back_populates="to_account"
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[CategoryKind] = mapped_column(SAEnum(CategoryKind))

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="category")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    type: Mapped[TransactionType] = mapped_column(SAEnum(TransactionType))

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    # Only set for transfers (e.g. paying down a credit card from checking).
    to_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    # Nullable because transfers don't need a spending category.
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    reimbursable: Mapped[bool] = mapped_column(Boolean, default=False)
    reimbursement_status: Mapped[ReimbursementStatus | None] = mapped_column(
        SAEnum(ReimbursementStatus), nullable=True
    )

    # A big one-off cost (tuition, a deposit) that shouldn't count toward
    # "living expenses" -- lets the dashboard show spend with and without
    # it, instead of one number that either hides or overstates it.
    exclude_from_living: Mapped[bool] = mapped_column(Boolean, default=False)

    # Money coming *in* that isn't earnings: a friend paying you back for
    # their share, a store refund, a card credit. Only meaningful on
    # INCOME rows. Balances treat it exactly like income (the money did
    # arrive), but summaries and trends subtract it from spending -- in
    # its own (expense) category -- instead of counting it as income, so
    # income means pay and spending means your actual share.
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set when this row came from (or was matched to) a Plaid transaction
    # -- unique, so a re-sync can never post the same bank transaction
    # twice. The pair id is the *other* side of a transfer between two
    # linked accounts (e.g. a card payment seen by both checking and the
    # card), so one row covers both without double-counting.
    plaid_transaction_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    plaid_pair_transaction_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    # Authorized at the bank but not yet posted. Counts toward balances
    # (it's real money you've spent), but the bank's *posted* balance
    # doesn't include it -- the sync's balance check accounts for that.
    pending: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped["Account"] = relationship(
        "Account", foreign_keys=[account_id], back_populates="transactions_from"
    )
    to_account: Mapped["Account | None"] = relationship(
        "Account", foreign_keys=[to_account_id], back_populates="transactions_to"
    )
    category: Mapped["Category | None"] = relationship(back_populates="transactions")


class ImportCapture(Base):
    """A durable log line for one bank sync (see app/plaid_sync.py): how
    many new transactions it posted and whether the bank's balance
    matched the app's afterwards. The sync alerts read the latest ones."""

    __tablename__ = "import_captures"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    transactions_found: Mapped[int] = mapped_column(Integer)
    bank_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    app_balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    balance_matches: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    account: Mapped["Account"] = relationship("Account", foreign_keys=[account_id])
