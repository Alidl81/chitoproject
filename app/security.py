from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import settings

_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def now_utc() -> datetime:
    return datetime.now(UTC)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def digest_token(token: str) -> str:
    key = settings.secret_key.get_secret_value().encode("utf-8")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    if not (
        8 <= len(password) <= 256
        and any(char.isupper() for char in password)
        and any(char.islower() for char in password)
        and any(char.isdecimal() for char in password)
        and any(char in string.punctuation for char in password)
    ):
        raise ValueError("رمز عبور باید ۸ تا ۲۵۶ نویسه و دست‌کم یک حرف بزرگ، یک حرف کوچک، یک عدد و یک علامت داشته باشد.")
    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _password_hasher.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def constant_time_token_match(candidate: str, expected_digest: str) -> bool:
    return hmac.compare_digest(digest_token(candidate), expected_digest)


def expires_in(*, minutes: int = 0, hours: int = 0) -> datetime:
    return now_utc() + timedelta(minutes=minutes, hours=hours)
