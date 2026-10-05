import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import LoginRequired

from urllib.parse import urlsplit

from app.database import ON_VERCEL, Base, SessionLocal, engine
from app.plaid_sync import sync_periodically
from app.routers import accounts, auth, dashboard, imports, plaid_routes, transactions, trends
from app.services import seed_default_categories

# No interactive API docs -- nothing uses them, and they'd publish the
# full endpoint map to anything that can reach the server.
app = FastAPI(title="Clearbook", docs_url=None, redoc_url=None, openapi_url=None)

# Listening on 127.0.0.1 keeps other machines out, but not other
# *websites*: any page open in your browser can send requests to
# localhost. The two layers below close that off. There's deliberately no
# CORS middleware -- with none, browsers refuse to let any other site read
# this server's responses.

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    """CSRF guard: a write must come from this app's own pages. Browsers
    always attach Origin to cross-site POSTs and a page can't forge it,
    so a mismatch means some other site is trying to act as you. No
    Origin at all = not a browser (e.g. curl on this machine), which a
    website can't drive."""
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        # Compare hosts, not full origins: behind Vercel's proxy the app
        # itself may see plain http while the browser's Origin is https.
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Standard hardening for a site that holds financial data: no framing
    (clickjacking), no MIME sniffing, no referrer leaking page URLs, and
    HTTPS-only once deployed."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ON_VERCEL:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(LoginRequired)
async def send_to_landing(request: Request, exc: LoginRequired):
    # A page visit goes to the landing page; anything else (form posts,
    # the Plaid fetch calls) just gets a plain "log in first".
    if request.method == "GET":
        return RedirectResponse(url="/landing", status_code=303)
    return PlainTextResponse("Log in first.", status_code=401)


# Login sessions: a cookie holding the user id, signed with SESSION_SECRET
# so it can't be forged or edited. Refuse to start without a real secret --
# a default one would let anyone mint a login cookie.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "SESSION_SECRET must be set to a long random string (32+ characters) -- "
        "see .env.example."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="clearbook_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    # Vercel sets VERCEL=1; there the cookie is HTTPS-only. Locally the
    # dev server is plain http://127.0.0.1, so it can't be.
    https_only=bool(os.getenv("VERCEL")),
)

# DNS rebinding guard: a malicious domain can re-resolve itself to
# 127.0.0.1 and slip past same-origin rules entirely. It still has to
# send its own name in the Host header, so only accept ours. Added last
# so it runs first.
# Allowed: localhost for development, plus the deployment's own Vercel
# addresses (Vercel sets these at runtime) and any custom domains listed in
# ALLOWED_HOSTS (comma-separated).
_allowed_hosts = ["127.0.0.1", "localhost"]
for var in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
    if os.getenv(var):
        _allowed_hosts.append(os.environ[var])
_allowed_hosts += [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(trends.router)
app.include_router(imports.router)
app.include_router(plaid_routes.router)


@app.on_event("startup")
def on_startup():
    # Dev-friendly: creates tables if they don't exist yet (SQLite by
    # default). For Prod we'll switch to real migrations (Alembic) once
    # the schema stabilizes -- fine to auto-create for now.
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_default_categories(db)
    finally:
        db.close()

    # Locally: post new bank transactions on launch and every few hours
    # after, in a background thread. On Vercel there's no long-running
    # process for a thread to live in -- syncing happens on login and
    # "Sync now" instead.
    if not ON_VERCEL:
        threading.Thread(target=sync_periodically, daemon=True).start()

