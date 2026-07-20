"""JWT helpers for server-validated anonymous customer identities."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import (
    MAXIMUM_JWT_EXPIRE_MINUTES,
    MINIMUM_JWT_SECRET_LENGTH,
    settings,
)


JWT_ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "token_version", "exp", "iat", "iss", "aud")


class InvalidCustomerToken(Exception):
    """Raised when an access token cannot represent a trusted customer."""


def _jwt_secret() -> str:
    secret = settings.AUTH_JWT_SECRET
    if not isinstance(secret, str) or len(secret) < MINIMUM_JWT_SECRET_LENGTH:
        raise RuntimeError(
            "Invalid AURA authentication configuration: AUTH_JWT_SECRET must be "
            f"configured and at least {MINIMUM_JWT_SECRET_LENGTH} characters long."
        )
    return secret


def _jwt_expire_minutes() -> int:
    expires_minutes = settings.AUTH_JWT_EXPIRE_MINUTES
    if (
        isinstance(expires_minutes, bool)
        or not isinstance(expires_minutes, int)
        or not 1 <= expires_minutes <= MAXIMUM_JWT_EXPIRE_MINUTES
    ):
        raise RuntimeError(
            "Invalid AURA authentication configuration: "
            "AUTH_JWT_EXPIRE_MINUTES must be a strict integer between 1 and "
            f"{MAXIMUM_JWT_EXPIRE_MINUTES}."
        )
    return expires_minutes


def create_customer_access_token(
    customer_id: UUID,
    token_version: int,
    *,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime]:
    """Create an HS256 token with the fixed AURA issuer and audience."""
    if isinstance(token_version, bool) or not isinstance(token_version, int):
        raise ValueError("token_version must be a positive integer")
    if token_version < 1:
        raise ValueError("token_version must be a positive integer")

    now = datetime.now(timezone.utc)
    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=_jwt_expire_minutes())
    )
    payload = {
        "sub": str(customer_id),
        "token_version": token_version,
        "iat": now,
        "exp": expires_at,
        "iss": settings.AUTH_JWT_ISSUER,
        "aud": settings.AUTH_JWT_AUDIENCE,
    }
    return (
        jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM),
        expires_at,
    )


def validate_customer_access_token(token: str) -> tuple[UUID, int]:
    """Validate all mandatory claims without exposing token details to callers."""
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            audience=settings.AUTH_JWT_AUDIENCE,
            issuer=settings.AUTH_JWT_ISSUER,
            options={"require": list(REQUIRED_CLAIMS)},
        )
        customer_id = UUID(str(payload["sub"]))
        token_version = payload["token_version"]
        if (
            isinstance(token_version, bool)
            or not isinstance(token_version, int)
            or token_version < 1
        ):
            raise ValueError("token_version must be positive")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise InvalidCustomerToken from error

    return customer_id, token_version
