from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


# Statement paste (Gemini-parsed) and its review queue were removed once
# every bank account was linked via Plaid; these keep old bookmarks from
# 404ing.
@router.get("/import")
@router.get("/import/review")
def old_import_pages():
    return RedirectResponse(url="/manual", status_code=303)
