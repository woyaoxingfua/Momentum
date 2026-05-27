from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone


def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2:{salt.hex()}:{key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(new_key, key)
    except (ValueError, AttributeError):
        return False


def generate_token() -> str:
    return secrets.token_hex(32)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
