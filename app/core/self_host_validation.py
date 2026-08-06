"""Secret-safe validation for the Windows loopback-only runtime."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


SELF_HOST_CONFIGURATION_ERROR = "SELF_HOST_CONFIGURATION_INVALID"
SELF_HOST_PROFILES = {"staging": 8001, "production": 8000}


class SelfHostConfigurationError(ValueError):
    def __init__(self):
        super().__init__(SELF_HOST_CONFIGURATION_ERROR)


@dataclass(frozen=True)
class SelfHostRuntime:
    profile: str
    host: str
    port: int


def validate_self_host_runtime(
    *,
    profile: object,
    app_env: object,
    bind_host: object,
    port: object,
    database_url: object,
) -> SelfHostRuntime:
    """Validate without including supplied values in any error."""
    if profile not in SELF_HOST_PROFILES:
        raise SelfHostConfigurationError()
    expected_port = SELF_HOST_PROFILES[profile]
    if app_env != "demo" or bind_host != "127.0.0.1":
        raise SelfHostConfigurationError()
    if isinstance(port, bool):
        raise SelfHostConfigurationError()
    try:
        parsed_port = int(port)
    except (TypeError, ValueError):
        raise SelfHostConfigurationError() from None
    if str(port) != str(parsed_port) or parsed_port != expected_port:
        raise SelfHostConfigurationError()
    if not isinstance(database_url, str) or not database_url:
        raise SelfHostConfigurationError()
    try:
        parsed_database = make_url(database_url)
        backend = parsed_database.get_backend_name().casefold()
        hostname = parsed_database.host
        database = parsed_database.database
    except (ArgumentError, AttributeError, TypeError, ValueError):
        raise SelfHostConfigurationError() from None
    if (
        backend not in {"postgres", "postgresql"}
        or hostname != "127.0.0.1"
        or parsed_database.port not in {None, 5432}
        or not isinstance(database, str)
        or "demo" not in database.casefold()
    ):
        raise SelfHostConfigurationError()
    return SelfHostRuntime(profile=profile, host=bind_host, port=parsed_port)
