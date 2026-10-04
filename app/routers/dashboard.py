from datetime import date

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import Transaction, User
from app.services import (
    get_all_balances,
    get_month_summary,
    get_pending_reimbursements,
    get_total_balance,
    get_trailing_average_expense,
    get_trailing_average_income,
    owned_account_ids,
    relative_time,
)
from app.plaid_sync import get_last_synced, get_sync_alerts
from app.templating import templates

router = APIRouter()


def _parse_month(value: str | None) -> date | None:
    if not value:
        return None
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return None


@router.get("/")
def dashboard(
    request: Request,
    account_id: str | None = None,
    month: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    today = date.today()
    this_month = date(today.year, today.month, 1)
    # A future month has nothing to show and no "typical" history to
    # anchor to -- cap at the current month rather than rendering an
    # empty page that looks broken.
    selected_month = min(_parse_month(month) or this_month, this_month)

    balances = get_all_balances(db, user_id=user.id)
    total_balance = get_total_balance(balances)

    # account_id arrives as a string because the "All accounts" option's
    # empty value (?account_id=) would otherwise fail int parsing outright
    # (FastAPI 422s on "" for an int param, missing or not).
    parsed_account_id = int(account_id) if account_id else None
    selected_account = next((a for a, _ in balances if a.id == parsed_account_id), None)
    # An account_id that doesn't match any real account (stale link, typo'd
    # URL) falls back to unfiltered rather than silently showing nothing.
    effective_account_id = selected_account.id if selected_account else None

    # Months to offer in the picker: from the user's first transaction to
    # now, newest first, labeled in words ("October 2026", not "2026-10").
    first_txn = db.scalar(
        select(func.min(Transaction.date)).where(Transaction.account_id.in_(owned_account_ids(user.id)))
    )
    first_month = date(first_txn.year, first_txn.month, 1) if first_txn else this_month
    month_options = []
    m = this_month
    while m >= min(first_month, selected_month):
        month_options.append({"value": m.strftime("%Y-%m"), "label": m.strftime("%B %Y")})
        m -= relativedelta(months=1)

    summary = get_month_summary(
        db, selected_month.year, selected_month.month, effective_account_id, user_id=user.id
    )

    expense_avg, expense_avg_months = get_trailing_average_expense(
        db, account_id=effective_account_id, as_of=selected_month, user_id=user.id
    )
    living_expense_avg, living_expense_avg_months = get_trailing_average_expense(
        db, account_id=effective_account_id, living_only=True, as_of=selected_month, user_id=user.id
    )
    income_avg, income_avg_months = get_trailing_average_income(
        db, account_id=effective_account_id, as_of=selected_month, user_id=user.id
    )
    living_income_avg, living_income_avg_months = get_trailing_average_income(
        db, account_id=effective_account_id, living_only=True, as_of=selected_month, user_id=user.id
    )

    pending = get_pending_reimbursements(db, effective_account_id, user_id=user.id)
    last_synced = get_last_synced(db, user_id=user.id)

    max_category = max(summary["by_category"].values()) if summary["by_category"] else 1
    max_category_living = (
        max(summary["by_category_living"].values()) if summary["by_category_living"] else 1
    )

    prev_month = selected_month - relativedelta(months=1)
    next_month = selected_month + relativedelta(months=1)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": summary,
            "has_plaid_keys": bool(user.plaid_secret),
            "balances": balances,
            "total_balance": total_balance,
            "selected_account_id": effective_account_id,
            "selected_account": selected_account,
            "expense_avg": expense_avg,
            "expense_avg_months": expense_avg_months,
            "living_expense_avg": living_expense_avg,
            "living_expense_avg_months": living_expense_avg_months,
            "income_avg": income_avg,
            "income_avg_months": income_avg_months,
            "living_income_avg": living_income_avg,
            "living_income_avg_months": living_income_avg_months,
            "pending": pending,
            "max_category": max_category,
            "max_category_living": max_category_living,
            "sync_alerts": get_sync_alerts(db, user_id=user.id),
            "last_synced": relative_time(last_synced) if last_synced else None,
            "month_name": selected_month.strftime("%B %Y"),
            "month_value": selected_month.strftime("%Y-%m"),
            "prev_month_value": prev_month.strftime("%Y-%m"),
            "next_month_value": next_month.strftime("%Y-%m"),
            "current_month_value": this_month.strftime("%Y-%m"),
            "is_current_month": selected_month == this_month,
            "month_options": month_options,
            "has_prev_month": selected_month > first_month,
        },
    )
