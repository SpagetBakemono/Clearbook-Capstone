from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    EMAIL_PATTERN,
    MIN_PASSWORD_LENGTH,
    clear_failures,
    current_user,
    find_user,
    hash_password,
    log_in,
    log_out,
    normalize_email,
    record_failure,
    too_many_attempts,
    verify_password,
)
from app.database import get_db
from app.models import User
from app.plaid_sync import sync_user_accounts
from app.templating import templates

router = APIRouter()

# Deliberately the same message for "no such email" and "wrong password",
# so the form can't be used to find out who has an account.
BAD_LOGIN = "Email or password is incorrect."


def _home_if_logged_in(request: Request, db: Session):
    return RedirectResponse(url="/", status_code=303) if current_user(request, db) else None


@router.get("/landing")
def landing(request: Request, db: Session = Depends(get_db)):
    return _home_if_logged_in(request, db) or templates.TemplateResponse(request, "landing.html", {})


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    return _home_if_logged_in(request, db) or templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    def fail(message: str):
        return templates.TemplateResponse(
            request, "login.html", {"error": message, "email": email}, status_code=400
        )

    if too_many_attempts(email):
        return fail("Too many attempts. Wait a few minutes and try again.")
    user = find_user(db, email)
    if user is None or not verify_password(password, user.password_hash, user.password_salt):
        record_failure(email)
        return fail(BAD_LOGIN)

    clear_failures(email)
    log_in(request, user)
    # Fresh bank data for this user, fetched after the redirect goes out.
    background.add_task(sync_user_accounts, user.id)
    return RedirectResponse(url="/", status_code=303)


@router.get("/signup")
def signup_form(request: Request, db: Session = Depends(get_db)):
    return _home_if_logged_in(request, db) or templates.TemplateResponse(request, "signup.html", {})


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    def fail(message: str):
        return templates.TemplateResponse(
            request, "signup.html", {"error": message, "email": email}, status_code=400
        )

    email_clean = normalize_email(email)
    if not EMAIL_PATTERN.match(email_clean):
        return fail("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        return fail(f"Use at least {MIN_PASSWORD_LENGTH} characters for your password.")
    if password != confirm:
        return fail("The two passwords don't match.")
    if find_user(db, email_clean) is not None:
        return fail("An account with that email already exists. Try logging in.")

    password_hash, salt = hash_password(password)
    user = User(email=email_clean, password_hash=password_hash, password_salt=salt)
    db.add(user)
    db.commit()
    log_in(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    log_out(request)
    return RedirectResponse(url="/landing", status_code=303)
