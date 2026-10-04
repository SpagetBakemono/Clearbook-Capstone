from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import User
from app.plaid_client import (
    create_link_token,
    describe_error,
    exchange_public_token,
    get_accounts,
    remove_item,
)
from app.plaid_sync import LAST_SYNC_ERRORS, NoPlaidKeys, creds_for, sync_account_recording_errors
from app.routers.accounts import owned_account
from app.token_crypto import decrypt_token, encrypt_token

router = APIRouter()


def _plaid_error(e: Exception) -> JSONResponse:
    # Surface the real reason instead of a bare 500, so a bad key or a
    # sandbox token used against production is diagnosable from the UI.
    return JSONResponse({"error": f"Plaid error: {describe_error(e)}"}, status_code=502)


@router.post("/plaid/create-link-token")
def plaid_create_link_token(user: User = Depends(require_user)):
    try:
        return {"link_token": create_link_token(creds_for(user), f"clearbook-user-{user.id}")}
    except NoPlaidKeys as e:
        return JSONResponse({"error": str(e), "setup_url": "/setup/plaid"}, status_code=400)
    except Exception as e:  # SDK, network (TLS/DNS/timeout), or missing config
        return _plaid_error(e)


class ExchangeRequest(BaseModel):
    public_token: str
    account_id: int
    # From Link's onSuccess metadata -- which account the user picked
    # inside Link. Optional because some institutions don't surface a
    # selection step, in which case the Item's only account is used.
    plaid_account_id: str | None = None


@router.post("/plaid/exchange")
def plaid_exchange(
    body: ExchangeRequest, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    account = owned_account(db, user, body.account_id)
    if account is None:
        return JSONResponse({"error": "That account no longer exists."}, status_code=400)

    try:
        creds = creds_for(user)
        access_token = exchange_public_token(creds, body.public_token)
        plaid_accounts = get_accounts(creds, access_token)
    except Exception as e:  # SDK, network (TLS/DNS/timeout), or missing config
        return _plaid_error(e)

    if body.plaid_account_id:
        match = next(
            (a for a in plaid_accounts if a["plaid_account_id"] == body.plaid_account_id), None
        )
    elif len(plaid_accounts) == 1:
        match = plaid_accounts[0]
    else:
        match = None

    if match is None:
        return JSONResponse(
            {"error": "Couldn't tell which account to link -- pick exactly one account in Plaid."},
            status_code=400,
        )

    try:
        account.plaid_access_token = encrypt_token(access_token)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    account.plaid_account_id = match["plaid_account_id"]
    # Fresh link, fresh history -- the first sync starts from the
    # beginning of what Plaid has for this account.
    account.plaid_cursor = None
    db.commit()
    return {"ok": True, "linked": match["name"]}


@router.post("/accounts/{account_id}/plaid/sync")
def plaid_sync(
    account_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    account = owned_account(db, user, account_id)
    if account is None or not account.plaid_access_token:
        return RedirectResponse(url="/accounts", status_code=303)
    # A failure or a balance mismatch shows in the alerts banner there.
    sync_account_recording_errors(db, account)
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/accounts/{account_id}/plaid/disconnect")
def plaid_disconnect(
    account_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Revokes the connection at Plaid, then forgets it locally. Also how
    you relink an account against real data after testing in sandbox."""
    account = owned_account(db, user, account_id)
    if account:
        if account.plaid_access_token:
            try:
                remove_item(creds_for(user), decrypt_token(account.plaid_access_token))
            except Exception as e:  # already revoked, wrong env, network...
                # Still clear it locally -- a token the app can't use is
                # worse than useless to keep -- but say so, since it may
                # still be live at Plaid (revoke it from the dashboard).
                print(f"[plaid] {account.name}: couldn't revoke at Plaid -- {describe_error(e)}", flush=True)
        LAST_SYNC_ERRORS.pop(account.id, None)
        account.plaid_access_token = None
        account.plaid_account_id = None
        account.plaid_cursor = None
        db.commit()
    return RedirectResponse(url="/accounts", status_code=303)
