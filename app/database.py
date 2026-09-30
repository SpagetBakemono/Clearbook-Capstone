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

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./expense_tracker.db"

# SQLite needs this flag when used with FastAPI's threaded request handling.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
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
