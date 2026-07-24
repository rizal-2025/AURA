"""JWT helpers for server-validated anonymous customer identities."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import (
    MAXIMUM_JWT_EXPIRE_MINUTES,
    get_auth_settings,
)
from app.core.config_validation import (
    CFG_AUTH_AUDIENCE_INVALID,
    CFG_AUTH_EXPIRY_INVALID,
    CFG_AUTH_ISSUER_INVALID,
    CFG_AUTH_SECRET_INVALID,
    ConfigurationError,
    parse_strict_positive_integer,
    validate_app_environment,
    validate_deployed_identity_label,
    validate_secret,
)


JWT_ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "token_version", "exp", "iat", "iss", "aud")


class InvalidCustomerToken(Exception):
    """Raised when an access token cannot represent a trusted customer."""


def _runtime_auth_settings():
    """Defensively revalidate cached/injected auth configuration."""
    configured = get_auth_settings()
    try:
        app_env = validate_app_environment(configured.APP_ENV)
        validate_secret(
            configured.AUTH_JWT_SECRET,
            code=CFG_AUTH_SECRET_INVALID,
        )
        parse_strict_positive_integer(
            configured.AUTH_JWT_EXPIRE_MINUTES,
            minimum=1,
            maximum=MAXIMUM_JWT_EXPIRE_MINUTES,
            code=CFG_AUTH_EXPIRY_INVALID,
        )
        validate_deployed_identity_label(
            configured.AUTH_JWT_ISSUER,
            app_env=app_env,
            development_value="aura",
            code=CFG_AUTH_ISSUER_INVALID,
        )
        validate_deployed_identity_label(
            configured.AUTH_JWT_AUDIENCE,
            app_env=app_env,
            development_value="aura-api",
            code=CFG_AUTH_AUDIENCE_INVALID,
        )
    except (AttributeError, ConfigurationError):
        raise RuntimeError("Invalid AURA authentication configuration.") from None
    return configured


def _jwt_secret(configured=None) -> str:
    configured = configured or _runtime_auth_settings()
    try:
        return validate_secret(
            configured.AUTH_JWT_SECRET,
            code=CFG_AUTH_SECRET_INVALID,
        )
    except (AttributeError, ConfigurationError):
        raise RuntimeError("Invalid AURA authentication configuration.") from None


def _jwt_expire_minutes(configured=None) -> int:
    configured = configured or _runtime_auth_settings()
    try:
        return parse_strict_positive_integer(
            configured.AUTH_JWT_EXPIRE_MINUTES,
            minimum=1,
            maximum=MAXIMUM_JWT_EXPIRE_MINUTES,
            code=CFG_AUTH_EXPIRY_INVALID,
        )
    except (AttributeError, ConfigurationError):
        raise RuntimeError("Invalid AURA authentication configuration.") from None


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

    configured = _runtime_auth_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=_jwt_expire_minutes(configured))
    )
    payload = {
        "sub": str(customer_id),
        "token_version": token_version,
        "iat": now,
        "exp": expires_at,
        "iss": configured.AUTH_JWT_ISSUER,
        "aud": configured.AUTH_JWT_AUDIENCE,
    }
    return (
        jwt.encode(payload, _jwt_secret(configured), algorithm=JWT_ALGORITHM),
        expires_at,
    )


def validate_customer_access_token(token: str) -> tuple[UUID, int]:
    """Validate all mandatory claims without exposing token details to callers."""
    try:
        auth_settings = _runtime_auth_settings()
        payload = jwt.decode(
            token,
            _jwt_secret(auth_settings),
            algorithms=[JWT_ALGORITHM],
            audience=auth_settings.AUTH_JWT_AUDIENCE,
            issuer=auth_settings.AUTH_JWT_ISSUER,
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
