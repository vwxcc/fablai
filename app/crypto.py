from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    secret = settings.app_secret.strip()
    if len(secret) < 32:
        raise RuntimeError("APP_SECRET must contain at least 32 characters")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise RuntimeError("Could not decrypt a stored provider credential; check APP_SECRET") from exc


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "••••••••" + value[-4:] if len(value) > 4 else "••••"
