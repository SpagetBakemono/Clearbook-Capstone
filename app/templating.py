"""Shared Jinja2Templates instance for all routers.

Registers static_version() as a template global so <link>/<script> tags can
cache-bust with the file's actual mtime (see base.html) -- without it,
browsers keep serving a stale cached style.css after every edit, since
nothing in the URL ever changes to tell them the file did.
"""
import os

from fastapi.templating import Jinja2Templates


templates = Jinja2Templates(directory="app/templates")


def static_version(filename: str) -> str:
    path = os.path.join("app/static", filename)
    return str(int(os.path.getmtime(path)))


templates.env.globals["static_version"] = static_version


def money(value, signed: bool = False) -> str:
    """$1,802.43 -- thousands separators, two decimals, minus sign before
    the dollar sign. signed=True also shows "+" on positives (for nets)."""
    amount = float(value or 0)
    sign = "-" if amount < 0 else ("+" if signed and amount > 0 else "")
    return f"{sign}${abs(amount):,.2f}"


templates.env.filters["money"] = money
