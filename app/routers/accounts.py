from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import Account, AccountType, ImportCapture, Transaction, TransactionType, User
from app.plaid_sync import get_balance_drift, get_last_synced, get_sync_alerts
from app.services import get_all_balances, relative_time
from app.templating import templates

router = APIRouter()


def owned_account(db: Session, user: User, account_id: int | None) -> Account | None:
    """The account if it exists AND belongs to this user -- otherwise None,
    exactly as if it didn't exist (so ids can't be probed)."""
    account = db.get(Account, account_id) if account_id is not None else None
    return account if account is not None and account.user_id == user.id else None


@router.get("/accounts")
def list_accounts(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    balances = get_all_balances(db, user_id=user.id)
    last_synced = get_last_synced(db, user_id=user.id)
    # Each linked account's own last sync, in words ("2 hours ago").
    last_sync_by_account = {
        account_id: relative_time(when)
        for account_id, when in db.execute(
            select(ImportCapture.account_id, func.max(ImportCapture.created_at))
            .where(ImportCapture.account_id.in_([a.id for a, _ in balances]))
            .group_by(ImportCapture.account_id)
        ).all()
    }
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "balances": balances,
            "has_plaid_keys": bool(user.plaid_secret),
            "plaid_env_label": "real banks" if user.plaid_env == "production" else "Sandbox test banks",
            "last_sync_by_account": last_sync_by_account,
            "sync_alerts": get_sync_alerts(db, user_id=user.id),
            "balance_drift": get_balance_drift(db, user_id=user.id),
            "last_synced": relative_time(last_synced) if last_synced else None,
        },
    )


@router.get("/accounts/new")
def new_account_form(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "account_new.html",
        {"account_types": list(AccountType), "today": date.today().isoformat()},
    )


@router.post("/accounts")
def create_account(
    name: str = Form(...),
    type: str = Form(...),
    opening_balance: str = Form("0"),
    opening_balance_date: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        balance = Decimal(opening_balance or "0")
    except InvalidOperation:
        balance = Decimal(0)

    account = Account(
        user_id=user.id,
        name=name.strip(),
        type=AccountType(type),
        opening_balance=balance,
        opening_balance_date=date.fromisoformat(opening_balance_date),
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=303)


@router.get("/accounts/{account_id}/edit")
def edit_account_form(
    account_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = owned_account(db, user, account_id)
    if account is None:
        return RedirectResponse(url="/accounts", status_code=303)
    return templates.TemplateResponse(
        request,
        "account_new.html",
        {"account_types": list(AccountType), "today": date.today().isoformat(), "account": account},
    )


@router.post("/accounts/{account_id}/edit")
def update_account(
    account_id: int,
    name: str = Form(...),
    type: str = Form(...),
    opening_balance: str = Form("0"),
    opening_balance_date: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = owned_account(db, user, account_id)
    if account is None:
        return RedirectResponse(url="/accounts", status_code=303)

    try:
        balance = Decimal(opening_balance or "0")
    except InvalidOperation:
        balance = Decimal(0)

    account.name = name.strip()
    account.type = AccountType(type)
    account.opening_balance = balance
    account.opening_balance_date = date.fromisoformat(opening_balance_date)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/accounts/{account_id}/delete")
def delete_account(
    account_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Deletes the account and every transaction on it (revoking its bank
    connection first). A transfer to or from one of the user's other
    accounts stays on that account as plain income/expense, so its
    balance doesn't change."""
    account = owned_account(db, user, account_id)
    if account is None:
        return RedirectResponse(url="/accounts", status_code=303)

    if account.plaid_access_token:
        from app.plaid_client import describe_error, remove_item
        from app.plaid_sync import LAST_SYNC_ERRORS, creds_for
        from app.token_crypto import decrypt_token

        try:
            remove_item(creds_for(user), decrypt_token(account.plaid_access_token))
        except Exception as e:  # already revoked, wrong env, no keys...
            print(f"[plaid] {account.name}: couldn't revoke at Plaid -- {describe_error(e)}", flush=True)
        LAST_SYNC_ERRORS.pop(account.id, None)

    # Money that left another account for this one: an expense there.
    db.execute(
        update(Transaction)
        .where(Transaction.to_account_id == account.id, Transaction.account_id != account.id)
        .values(to_account_id=None, type=TransactionType.EXPENSE)
    )
    # Money that came from this one into another: income there.
    db.execute(
        update(Transaction)
        .where(Transaction.account_id == account.id, Transaction.to_account_id.is_not(None),
               Transaction.to_account_id != account.id)
        .values(account_id=Transaction.to_account_id, to_account_id=None, type=TransactionType.INCOME)
    )
    db.execute(delete(Transaction).where(
        or_(Transaction.account_id == account.id, Transaction.to_account_id == account.id)))
    db.execute(delete(ImportCapture).where(ImportCapture.account_id == account.id))
    db.delete(account)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=303)
