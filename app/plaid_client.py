"""
Plaid client wrapper -- creates Link tokens, exchanges them for access
tokens, and syncs transactions. Every call takes the *user's own* Plaid
Sandbox keys (PlaidCreds) -- Clearbook has no Plaid keys of its own, by
design. A fresh client per call, no DB access here; app/plaid_sync.py
decides what happens with the result.
"""
import json
from dataclasses import dataclass
from decimal import Decimal

import certifi
import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

@dataclass(frozen=True)
class PlaidCreds:
    """One user's Plaid Sandbox keys (decrypted, in memory only)."""

    client_id: str
    secret: str

    def __repr__(self) -> str:  # never let the secret reach a log line
        return f"PlaidCreds(client_id={self.client_id!r}, secret=***)"


def _client(creds: PlaidCreds) -> plaid_api.PlaidApi:
    # Sandbox only: test banks and fake transactions, no real bank data.
    configuration = plaid.Configuration(
        host=plaid.Environment.Sandbox,
        api_key={"clientId": creds.client_id, "secret": creds.secret},
    )
    # python.org's macOS Python ships without a trusted CA bundle, so TLS
    # verification fails against plaid.com. Point it at certifi's bundle --
    # never switch verification off, which would let anyone on the network
    # impersonate Plaid and harvest these credentials.
    configuration.ssl_ca_cert = certifi.where()
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def describe_error(e: Exception) -> str:
    """One readable line for the UI. Plaid API errors carry a JSON body
    with error_code/error_message; anything else (network, config) is
    just its own message. Neither ever contains the client secret or an
    access token -- those only travel in the request."""
    body = getattr(e, "body", None)
    if body:
        try:
            data = json.loads(body)
            return f"{data.get('error_code', 'PLAID_ERROR')}: {data.get('error_message', '').strip()}"
        except (ValueError, TypeError):
            pass
    return str(e)


def create_link_token(creds: PlaidCreds, user_ref: str) -> str:
    """A short-lived token the frontend uses to open Plaid Link. One
    generic session covers whichever bank/account the user picks in the
    Link UI -- mapping the result to a specific local Account happens
    after, in the exchange step."""
    client = _client(creds)
    request = LinkTokenCreateRequest(
        client_name="Clearbook",
        language="en",
        country_codes=[CountryCode("US")],
        user=LinkTokenCreateRequestUser(client_user_id=user_ref),
        products=[Products("transactions")],
    )
    return client.link_token_create(request).link_token


def exchange_public_token(creds: PlaidCreds, public_token: str) -> str:
    """Public tokens are single-use and expire in ~30 minutes; the
    access_token this returns is the long-lived credential that actually
    gets stored (on Account.plaid_access_token)."""
    client = _client(creds)
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    return client.item_public_token_exchange(request).access_token


def remove_item(creds: PlaidCreds, access_token: str) -> None:
    """Revokes the connection at Plaid: the access token stops working
    for good, and (on the trial) the connection slot frees up. Just
    forgetting the token locally would leave both alive."""
    _client(creds).item_remove(ItemRemoveRequest(access_token=access_token))


def get_accounts(creds: PlaidCreds, access_token: str) -> list[dict]:
    """The account(s) available under one linked Item, so a caller can
    show a picker if Link surfaced more than one (e.g. checking +
    savings from the same bank in one session)."""
    client = _client(creds)
    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)
    return [
        {
            "plaid_account_id": a.account_id,
            "name": a.name,
            "official_name": a.official_name,
            "type": str(a.type),
            "subtype": str(a.subtype) if a.subtype else None,
            "mask": a.mask,
        }
        for a in response.accounts
    ]


def get_balance(creds: PlaidCreds, access_token: str, plaid_account_id: str) -> Decimal | None:
    """Live current balance for one account under an Item -- used to
    cross-check against the app's own projected balance after a sync,
    the same way a statement's stated balance already is."""
    client = _client(creds)
    request = AccountsBalanceGetRequest(access_token=access_token)
    response = client.accounts_balance_get(request)
    for a in response.accounts:
        if a.account_id == plaid_account_id and a.balances.current is not None:
            return Decimal(str(a.balances.current))
    return None


def _shape(t) -> dict:
    # Plaid's sign convention: positive amount = money left the account,
    # regardless of account type. "outflow" is recorded explicitly so the
    # caller maps it onto this app's own model (where a card purchase is
    # an EXPENSE that *increases* what's owed).
    amount = Decimal(str(t.amount))
    # authorized_date is when you actually bought it -- what statements
    # and hand-entered rows use. `date` is the later posting date, which
    # would sit 1-3 days off every manual entry and defeat matching.
    when = t.authorized_date or t.date
    pfc = t.personal_finance_category
    return {
        "plaid_id": t.transaction_id,
        "pending_plaid_id": t.pending_transaction_id,
        "date": when,
        "amount": abs(amount),
        "outflow": amount > 0,
        "merchant": t.merchant_name or t.name or "(unknown)",
        "pending": bool(t.pending),
        "category_primary": pfc.primary if pfc else None,
        "category_detailed": pfc.detailed if pfc else None,
    }


def sync_transactions(
    creds: PlaidCreds, access_token: str, plaid_account_id: str, cursor: str | None
) -> dict:
    """Wraps /transactions/sync for one account within an Item (an Item
    can cover several; everything is filtered to plaid_account_id).
    Follows has_more to the end so the returned cursor is only ever a
    complete bookmark.

    Returns {added, modified: [shaped dicts], removed: [plaid ids],
    next_cursor}. Pending transactions are included -- when one posts,
    Plaid removes the pending id and adds a posted one whose
    pending_plaid_id points back at it, which the caller uses to update
    the same row in place."""
    client = _client(creds)
    added, modified, removed = [], [], []
    next_cursor = cursor
    has_more = True

    while has_more:
        request = TransactionsSyncRequest(access_token=access_token, cursor=next_cursor or "")
        response = client.transactions_sync(request)
        added += [_shape(t) for t in response.added if t.account_id == plaid_account_id]
        modified += [_shape(t) for t in response.modified if t.account_id == plaid_account_id]
        removed += [t.transaction_id for t in response.removed if t.account_id == plaid_account_id]
        next_cursor = response.next_cursor
        has_more = response.has_more

    return {"added": added, "modified": modified, "removed": removed, "next_cursor": next_cursor}
