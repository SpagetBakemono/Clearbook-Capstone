import re

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
    require_user,
    too_many_attempts,
    verify_password,
)
from app.database import get_db
from app.demo import create_demo_user, delete_demo_users, delete_stale_demo_users
from app.models import User
from app.plaid_client import PlaidCreds, create_link_token, describe_error
from app.plaid_sync import sync_user_accounts
from app.token_crypto import encrypt_token
from app.templating import templates

router = APIRouter()

# Deliberately the same message for "no such email" and "wrong password",
# so the form can't be used to find out who has an account.
BAD_LOGIN = "Email or password is incorrect."


def _home_if_logged_in(request: Request, db: Session):
    # A demo visitor is welcome on Log in / Sign up -- that's how they leave
    # the demo for a real account.
    user = current_user(request, db)
    return RedirectResponse(url="/", status_code=303) if user and not user.is_demo else None


def _end_demo_if_any(request: Request, db: Session) -> None:
    """Switching from the demo to a real account: the demo's made-up data
    has no further use, so delete it now rather than after 24h."""
    user = current_user(request, db)
    if user is not None and user.is_demo:
        delete_demo_users(db, [user.id])


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
    _end_demo_if_any(request, db)
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

    _end_demo_if_any(request, db)
    password_hash, salt = hash_password(password)
    user = User(email=email_clean, password_hash=password_hash, password_salt=salt)
    db.add(user)
    db.commit()
    log_in(request, user)
    # Next step for a new user: their own Plaid keys.
    return RedirectResponse(url="/setup/plaid", status_code=303)


@router.post("/demo")
def start_demo(request: Request, db: Session = Depends(get_db)):
    """A fresh, private demo account full of made-up data (app/demo.py)."""
    delete_stale_demo_users(db)
    user = create_demo_user(db)
    log_in(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user is not None and user.is_demo:
        delete_demo_users(db, [user.id])  # a finished demo has no further use
    log_out(request)
    return RedirectResponse(url="/landing", status_code=303)


# ---- Plaid keys (each user brings their own Sandbox keys) -------------------

KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,64}$")


def _friendly_plaid_error(e: Exception) -> str:
    """Plaid's error codes, in words a person can act on. Never echoes the
    keys themselves."""
    code = describe_error(e).split(":", 1)[0]
    if code in ("INVALID_API_KEYS", "INVALID_CLIENT_ID", "INVALID_SECRET"):
        return (
            "Plaid didn't accept those keys. Check you copied the Client ID and the "
            "Sandbox secret (not the Production one) from the same team."
        )
    if code == "UNAUTHORIZED_ENVIRONMENT":
        return "Those keys aren't enabled for Sandbox. Copy the Sandbox secret from the Plaid dashboard."
    return "Couldn't reach Plaid to check the keys. Try again in a moment."


@router.get("/setup/plaid")
def plaid_setup_form(request: Request, user: User = Depends(require_user)):
    if user.is_demo:  # the demo runs on made-up data, never on Plaid
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "plaid_setup.html",
        {"has_keys": bool(user.plaid_secret), "client_id": user.plaid_client_id},
    )


@router.post("/setup/plaid")
def plaid_setup(
    request: Request,
    client_id: str = Form(""),
    secret: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if user.is_demo:
        return RedirectResponse(url="/", status_code=303)
    client_id, secret = client_id.strip(), secret.strip()

    def fail(message: str):
        return templates.TemplateResponse(
            request,
            "plaid_setup.html",
            {"error": message, "client_id": client_id, "has_keys": bool(user.plaid_secret)},
            status_code=400,
        )

    if not KEY_PATTERN.match(client_id) or not KEY_PATTERN.match(secret):
        return fail("That doesn't look like a Plaid key -- both are long strings of letters and numbers.")

    # Prove the keys work before saving them: a link token is the cheapest
    # call that needs a valid client ID + Sandbox secret pair.
    try:
        create_link_token(PlaidCreds(client_id, secret), f"clearbook-user-{user.id}")
    except Exception as e:  # Plaid rejected them, or network trouble
        return fail(_friendly_plaid_error(e))

    user.plaid_client_id = client_id
    user.plaid_secret = encrypt_token(secret)
    db.commit()
    return RedirectResponse(url="/", status_code=303)
