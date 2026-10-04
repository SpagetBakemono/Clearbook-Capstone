"""
Accounts and sessions for Clearbook's users.

- Passwords: never stored. Only a scrypt hash with a per-user random salt;
  checking compares in constant time.
- Sessions: Starlette's SessionMiddleware keeps `user_id` in a cookie
  signed with SESSION_SECRET (see app/main.py) -- a visitor can read
  nothing useful from it and can't forge or edit it.
- `require_user` is the gate every app page depends on. A logged-out visit
  raises LoginRequired, which app/main.py turns into a redirect to the
  landing page (or a 401 for non-page requests like the Plaid fetches).
"""
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

# scrypt cost: ~16MB memory, tens of ms per check -- slow enough to make
# guessing expensive, fast enough for a login form.
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=64)

MIN_PASSWORD_LENGTH = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginRequired(Exception):
    """Raised by require_user; handled in app/main.py."""


def hash_password(password: str) -> tuple[str, str]:
    """Returns (hash_hex, salt_hex)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), hash_hex)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def find_user(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def log_in(request: Request, user: User) -> None:
    # A fresh session on every login, so nothing from before it carries over.
    request.session.clear()
    request.session["user_id"] = user.id


def log_out(request: Request) -> None:
    request.session.clear()


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency for every page that shows a user's data."""
    user = current_user(request, db)
    if user is None:
        raise LoginRequired()
    request.state.user = user  # lets base.html show who's logged in
    return user


# ---- Light brute-force protection -----------------------------------------
# A demo-appropriate guard: at most MAX_FAILURES failed logins per email per
# window. In memory, so it's per server instance (best-effort on serverless)
# -- enough to stop a casual script, not a determined attacker.
MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60
_failures: dict[str, deque] = defaultdict(deque)


def too_many_attempts(email: str) -> bool:
    attempts = _failures[normalize_email(email)]
    cutoff = time.monotonic() - WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= MAX_FAILURES


def record_failure(email: str) -> None:
    _failures[normalize_email(email)].append(time.monotonic())


def clear_failures(email: str) -> None:
    _failures.pop(normalize_email(email), None)
