from datetime import date

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import Category, CategoryKind, Transaction, TransactionType, User
from app.services import get_balance_history, get_monthly_category_trend, owned_account_ids
from app.templating import templates

router = APIRouter()

MAX_MONTHS = 36  # keeps the chart from rendering hundreds of unreadable bars

GRANULARITIES = [("day", "Daily"), ("week", "Weekly"), ("month", "Monthly")]


def _parse_month(value: str | None) -> date | None:
    if not value:
        return None
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return None


@router.get("/trends")
def trends(
    request: Request,
    category_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    categories = db.scalars(
        select(Category).where(Category.kind == CategoryKind.EXPENSE).order_by(Category.name)
    ).all()

    # No category_id (missing, or the "All categories" option's empty
    # value) means don't split anything out -- every expense counts as
    # one series, not a category-vs-rest breakdown.
    selected_category_id = int(category_id) if category_id else None
    selected_category = next((c for c in categories if c.id == selected_category_id), None)

    today = date.today()
    this_month = date(today.year, today.month, 1)

    end_month = _parse_month(end) or this_month

    start_month = _parse_month(start)
    if start_month is None:
        earliest_expense = db.scalar(
            select(func.min(Transaction.date)).where(
                Transaction.type == TransactionType.EXPENSE,
                Transaction.account_id.in_(owned_account_ids(user.id)),
            )
        )
        start_month = (
            date(earliest_expense.year, earliest_expense.month, 1)
            if earliest_expense
            else end_month - relativedelta(months=5)
        )

    # A picker that's backwards or absurdly wide would otherwise render a
    # chart nobody can read -- swap and cap instead of erroring.
    if start_month > end_month:
        start_month, end_month = end_month, start_month
    months = (end_month.year - start_month.year) * 12 + (end_month.month - start_month.month) + 1
    if months > MAX_MONTHS:
        start_month = end_month - relativedelta(months=MAX_MONTHS - 1)
        months = MAX_MONTHS

    # Plain data for app/static/charts.js, which draws the axes, bars and
    # tooltips client-side. Living first everywhere: it's the view the
    # user reads first, Total is the drill-out.
    def stacked(kind, living_only, category_id=None):
        return get_monthly_category_trend(
            db,
            start_month,
            months,
            kind=kind,
            living_only=living_only,
            category_id=category_id,
            user_id=user.id,
        )

    charts = {
        "expense_living": stacked(CategoryKind.EXPENSE, True, selected_category_id),
        "expense_total": stacked(CategoryKind.EXPENSE, False, selected_category_id),
        "income_living": stacked(CategoryKind.INCOME, True),
        "income_total": stacked(CategoryKind.INCOME, False),
    }
    for g, _ in GRANULARITIES:
        charts[f"balance_{g}"] = [
            {"date": p["date"].isoformat(), "balance": float(p["balance"])}
            for p in get_balance_history(db, start_month, end_month, g, user_id=user.id)
        ]

    return templates.TemplateResponse(
        request,
        "trends.html",
        {
            "categories": categories,
            "selected_category_id": selected_category_id,
            "selected_category": selected_category,
            "start_value": start_month.strftime("%Y-%m"),
            "end_value": end_month.strftime("%Y-%m"),
            "charts": charts,
            "granularities": GRANULARITIES,
        },
    )
