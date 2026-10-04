from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import Account, AccountType, User
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
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "balances": balances,
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
