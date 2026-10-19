"""Password hashing (Argon2id), JWT access tokens and opaque rotating refresh tokens.

Adapted from the Finguard-v3 implementation.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from finguard.core.config import Settings

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)
_DUMMY_HASH = _hasher.hash("timing-equalization-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-work verification: a dummy hash is checked when the user does not exist."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_policy(password: str, min_length: int) -> list[str]:
    problems = []
    if len(password) < min_length:
        problems.append(f"must be at least {min_length} characters")
    if len(password) > 256:
        problems.append("must be at most 256 characters")
    classes = sum(bool(any(f(c) for c in password)) for f in (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    if classes < 3:
        problems.append("must contain at least three of: lowercase, uppercase, digit, symbol")
    return problems


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    org_id: str
    session_id: str
    jti: str
    expires_at: datetime


class TokenError(Exception):
    pass


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def issue_access(self, user_id: uuid.UUID, org_id: uuid.UUID, session_id: uuid.UUID) -> tuple[str, datetime]:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self._s.access_token_ttl_seconds)
        token = jwt.encode({
            "sub": str(user_id), "org": str(org_id), "sid": str(session_id), "jti": uuid.uuid4().hex, "type": "access",
            "iat": int(now.timestamp()), "nbf": int(now.timestamp()), "exp": int(exp.timestamp()),
            "iss": self._s.jwt_issuer, "aud": self._s.jwt_audience,
        }, self._s.jwt_secret, algorithm="HS256")
        return token, exp

    def decode_access(self, token: str) -> AccessClaims:
        try:
            data = jwt.decode(token, self._s.jwt_secret, algorithms=["HS256"], audience=self._s.jwt_audience,
                              issuer=self._s.jwt_issuer,
                              options={"require": ["exp", "iat", "nbf", "sub", "jti", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            raise TokenError("invalid token") from exc
        if data.get("type") != "access":
            raise TokenError("wrong token type")
        return AccessClaims(data["sub"], data["org"], data["sid"], data["jti"],
                            datetime.fromtimestamp(data["exp"], timezone.utc))


def new_refresh_token() -> tuple[str, str]:
    """Returns (opaque token for the client, SHA-256 hash for storage)."""
    token = secrets.token_urlsafe(48)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
