from fastapi import APIRouter, Form, Request

from app.templating import templates

router = APIRouter()

# Screens only for now -- accounts and sessions arrive with the auth step
# (CLAUDE.md TODO #2). Until then a submit just says so.
COMING_SOON = "Accounts are coming soon -- this screen isn't wired up yet."


@router.get("/landing")
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login(request: Request, email: str = Form("")):
    return templates.TemplateResponse(request, "login.html", {"notice": COMING_SOON, "email": email})


@router.get("/signup")
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {})


@router.post("/signup")
def signup(request: Request, email: str = Form("")):
    return templates.TemplateResponse(request, "signup.html", {"notice": COMING_SOON, "email": email})
