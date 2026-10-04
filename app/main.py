import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import LoginRequired

from app.database import Base, SessionLocal, engine
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
        own_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != own_origin:
            return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)


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
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

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

    # Post new bank transactions on launch and every few hours after.
    # Background thread so the page opens immediately instead of waiting
    # on Plaid; daemon so it never holds up shutdown.
    threading.Thread(target=sync_periodically, daemon=True).start()
