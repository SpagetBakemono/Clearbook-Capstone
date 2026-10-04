"""
Database setup.

Dev: defaults to a local SQLite file (expense_tracker.db) so there's zero
setup to run this on your machine.

Prod: set DATABASE_URL in the environment (e.g. a Postgres URL) and this
picks it up automatically -- no code change needed between environments.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()

ON_VERCEL = bool(os.getenv("VERCEL"))

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
if not DATABASE_URL:
    if ON_VERCEL:
        # Vercel's disk doesn't persist between requests -- a SQLite file
        # there would silently lose every sign-up. Fail loudly instead.
        raise RuntimeError("DATABASE_URL is not set -- add the Postgres database in Vercel (Storage).")
    DATABASE_URL = "sqlite:///./dev.db"

# Neon/Vercel hand out postgres:// or postgresql:// URLs; use the psycopg 3
# driver explicitly (it's the one in requirements.txt).
for prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(prefix):]

if DATABASE_URL.startswith("sqlite"):
    # SQLite needs this flag when used with FastAPI's threaded request handling.
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Serverless: each function instance is short-lived, so don't hold a
    # connection pool open -- Neon's own pooler does that job. pre_ping
    # drops a connection Neon closed while the instance sat idle.
    engine = create_engine(DATABASE_URL, poolclass=NullPool, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
