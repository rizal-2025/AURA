"""Secret-safe dependencies for the internal BFF-to-AURA demo boundary."""

import hashlib
import hmac
import re
from typing import Annotated

from fastapi import Depends, Header
from pydantic import SecretStr

from app.core.config import DemoSettings, get_demo_settings
from app.services.demo_session_service import (
    DemoServiceAuthRequiredError,
    DemoSessionRequiredError,
    DemoSessionService,
    demo_session_service,
    validate_demo_session_token,
)
from app.services.demo_rate_limit_service import (
    DemoRateLimitService,
    demo_rate_limit_service,
)


_DEMO_CLIENT_SUBJECT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def digest_demo_service_token(raw_token: str) -> bytes:
    """Return a fixed-length digest without retaining or rendering the token."""
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def require_demo_service_auth(
    service_token: Annotated[
        str | None,
        Header(alias="X-BFF-Service-Token"),
    ] = None,
    settings: DemoSettings = Depends(get_demo_settings),
) -> None:
    configured = settings.DEMO_BFF_SERVICE_TOKEN
    if (
        settings.APP_ENV != "demo"
        or not isinstance(configured, SecretStr)
        or not isinstance(service_token, str)
        or not hmac.compare_digest(
            digest_demo_service_token(service_token),
            digest_demo_service_token(configured.get_secret_value()),
        )
    ):
        raise DemoServiceAuthRequiredError()


def require_demo_session_token(
    session_token: Annotated[
        str | None,
        Header(alias="X-Demo-Session-Token"),
    ] = None,
) -> str:
    if session_token is None:
        raise DemoSessionRequiredError()
    return validate_demo_session_token(session_token)


def require_demo_client_subject(
    client_subject: Annotated[
        str | None,
        Header(alias="X-Demo-Client-Subject"),
    ] = None,
) -> str:
    """Accept only the BFF-derived opaque digest, never a raw address."""
    if (
        not isinstance(client_subject, str)
        or _DEMO_CLIENT_SUBJECT_PATTERN.fullmatch(client_subject) is None
    ):
        raise DemoServiceAuthRequiredError()
    return client_subject


def get_demo_session_service() -> DemoSessionService:
    return demo_session_service


def get_demo_rate_limit_service() -> DemoRateLimitService:
    """Provide the limiter.

    X-RateLimit-Reset uses Unix epoch seconds in UTC.
    """
    return demo_rate_limit_service
