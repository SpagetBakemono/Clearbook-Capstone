"""
Encrypts Plaid access tokens before they touch the database.

The key (PLAID_TOKEN_KEY) lives in .env, not the db -- so a copy of
expense_tracker.db on its own (a backup, a synced folder, an accidental
share) doesn't hand over working bank credentials.
"""
import os

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()


def _fernet() -> Fernet:
    key = os.getenv("PLAID_TOKEN_KEY")
    if not key:
        raise RuntimeError("PLAID_TOKEN_KEY is not set -- check .env")
    return Fernet(key.encode())


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Couldn't decrypt the stored Plaid token -- PLAID_TOKEN_KEY changed? "
            "Disconnect and relink the account."
        ) from e
