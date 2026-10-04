from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import (
    Category,
    ReimbursementStatus,
    Transaction,
    TransactionType,
    User,
)
from app.routers.accounts import owned_account
from app.services import get_user_accounts
from app.templating import templates

router = APIRouter()


def owned_transaction(db: Session, user: User, transaction_id: int) -> Transaction:
    """The transaction if it's on one of this user's accounts; 404 otherwise
    -- the same answer as for an id that doesn't exist, so ids can't be
    probed."""
    txn = db.get(Transaction, transaction_id)
    if txn is None or txn.account.user_id != user.id:
        raise HTTPException(status_code=404)
    return txn


def _require_own_accounts(
    db: Session, user: User, account_id: int, to_account_id: int | None
) -> None:
    """A form can be edited to post any account id; only the user's own
    accounts are accepted, on both sides of a transfer."""
    if owned_account(db, user, account_id) is None:
        raise HTTPException(status_code=404)
    if to_account_id is not None and owned_account(db, user, to_account_id) is None:
        raise HTTPException(status_code=404)


def _categories(db: Session) -> list[Category]:
    return db.scalars(select(Category).order_by(Category.name)).all()


def manual_page(request: Request, db: Session, user: User):
    """The Manual page: add a transaction by hand -- for cash, or anything
    the bank sync can't see. Everything else arrives on its own."""
    return templates.TemplateResponse(
        request,
        "manual.html",
        {
            "accounts": get_user_accounts(db, user.id),
            "categories": _categories(db),
            "today": date.today().isoformat(),
        },
    )


@router.get("/manual")
def manual(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return manual_page(request, db, user)


@router.get("/transactions/new")
def new_transaction_form():
    return RedirectResponse(url="/manual", status_code=303)


@router.post("/transactions")
def create_transaction(
    date_: str = Form(..., alias="date"),
    amount: str = Form(...),
    type: str = Form(...),
    account_id: int = Form(...),
    to_account_id: str = Form(""),
    category_id: str = Form(""),
    note: str = Form(""),
    reimbursable: str = Form(""),
    # Checkbox is "counts as living": present when ticked, absent when not.
    counts_as_living: str = Form(""),
    is_refund: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    txn_type = TransactionType(type)
    to_id = int(to_account_id) if (txn_type == TransactionType.TRANSFER and to_account_id) else None
    _require_own_accounts(db, user, account_id, to_id)

    txn = Transaction(
        date=date.fromisoformat(date_),
        amount=amount,
        type=txn_type,
        account_id=account_id,
        to_account_id=to_id,
        category_id=int(category_id) if category_id else None,
        note=note.strip() or None,
        reimbursable=bool(reimbursable) and txn_type == TransactionType.EXPENSE,
        is_refund=bool(is_refund) and txn_type == TransactionType.INCOME,
        exclude_from_living=not counts_as_living
        and txn_type in (TransactionType.EXPENSE, TransactionType.INCOME),
    )
    if txn.reimbursable:
        txn.reimbursement_status = ReimbursementStatus.PENDING

    db.add(txn)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/transactions/{transaction_id}/edit")
def edit_transaction_form(
    transaction_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    txn = owned_transaction(db, user, transaction_id)
    return templates.TemplateResponse(
        request,
        "transaction_new.html",
        {
            "accounts": get_user_accounts(db, user.id),
            "categories": _categories(db),
            "today": date.today().isoformat(),
            "txn": txn,
        },
    )


@router.post("/transactions/{transaction_id}/edit")
def update_transaction(
    transaction_id: int,
    date_: str = Form(..., alias="date"),
    amount: str = Form(...),
    type: str = Form(...),
    account_id: int = Form(...),
    to_account_id: str = Form(""),
    category_id: str = Form(""),
    note: str = Form(""),
    reimbursable: str = Form(""),
    # Checkbox is "counts as living": present when ticked, absent when not.
    counts_as_living: str = Form(""),
    is_refund: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    txn = owned_transaction(db, user, transaction_id)
    txn_type = TransactionType(type)
    to_id = int(to_account_id) if (txn_type == TransactionType.TRANSFER and to_account_id) else None
    _require_own_accounts(db, user, account_id, to_id)

    was_reimbursable = txn.reimbursable

    txn.date = date.fromisoformat(date_)
    txn.amount = amount
    txn.type = txn_type
    txn.account_id = account_id
    txn.to_account_id = to_id
    txn.category_id = int(category_id) if category_id else None
    txn.note = note.strip() or None
    txn.reimbursable = bool(reimbursable) and txn_type == TransactionType.EXPENSE
    txn.is_refund = bool(is_refund) and txn_type == TransactionType.INCOME
    txn.exclude_from_living = not counts_as_living and txn_type in (
        TransactionType.EXPENSE,
        TransactionType.INCOME,
    )

    # Only (re)open a reimbursement when it's newly marked reimbursable --
    # editing an already-pending or already-received one shouldn't reset
    # its status back to pending.
    if txn.reimbursable and not was_reimbursable:
        txn.reimbursement_status = ReimbursementStatus.PENDING
    elif not txn.reimbursable:
        txn.reimbursement_status = None

    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/transactions/{transaction_id}/delete")
def delete_transaction(
    transaction_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    db.delete(owned_transaction(db, user, transaction_id))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/transactions/{transaction_id}/mark-reimbursed")
def mark_reimbursed(
    transaction_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Flip a pending reimbursement to received. Logging the actual incoming
    cash as its own income transaction is a separate, deliberate step --
    this just closes out the receivable."""
    owned_transaction(db, user, transaction_id).reimbursement_status = ReimbursementStatus.RECEIVED
    db.commit()
    return RedirectResponse(url="/", status_code=303)
