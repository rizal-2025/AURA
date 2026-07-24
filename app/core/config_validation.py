"""Pure, side-effect-free validation helpers for AURA configuration."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit


VALID_APP_ENVIRONMENTS = frozenset(
    {"development", "test", "staging", "production"}
)
DEPLOYED_APP_ENVIRONMENTS = frozenset({"staging", "production"})

MINIMUM_SECRET_LENGTH = 32
MAXIMUM_SECRET_LENGTH = 512
MAXIMUM_JWT_EXPIRE_MINUTES = 1440
MAXIMUM_CONFIGURATION_LABEL_LENGTH = 128
MAXIMUM_PROVIDER_URL_LENGTH = 2048
MAXIMUM_TELEGRAM_TOKEN_LENGTH = 256

CFG_ENV_INVALID = "CFG_ENV_INVALID"
CFG_DATABASE_INVALID = "CFG_DATABASE_INVALID"
CFG_AUTH_SECRET_INVALID = "CFG_AUTH_SECRET_INVALID"
CFG_AUTH_EXPIRY_INVALID = "CFG_AUTH_EXPIRY_INVALID"
CFG_AUTH_ISSUER_INVALID = "CFG_AUTH_ISSUER_INVALID"
CFG_AUTH_AUDIENCE_INVALID = "CFG_AUTH_AUDIENCE_INVALID"
CFG_AI_PROVIDER_INVALID = "CFG_AI_PROVIDER_INVALID"
CFG_AI_OPENAI_INVALID = "CFG_AI_OPENAI_INVALID"
CFG_AI_OLLAMA_INVALID = "CFG_AI_OLLAMA_INVALID"
CFG_TELEGRAM_TOKEN_INVALID = "CFG_TELEGRAM_TOKEN_INVALID"
CFG_TELEGRAM_IDENTITY_INVALID = "CFG_TELEGRAM_IDENTITY_INVALID"
CFG_TELEGRAM_OWNER_INVALID = "CFG_TELEGRAM_OWNER_INVALID"
CFG_TELEGRAM_OPTION_INVALID = "CFG_TELEGRAM_OPTION_INVALID"

SAFE_CONFIGURATION_CODES = frozenset(
    {
        CFG_ENV_INVALID,
        CFG_DATABASE_INVALID,
        CFG_AUTH_SECRET_INVALID,
        CFG_AUTH_EXPIRY_INVALID,
        CFG_AUTH_ISSUER_INVALID,
        CFG_AUTH_AUDIENCE_INVALID,
        CFG_AI_PROVIDER_INVALID,
        CFG_AI_OPENAI_INVALID,
        CFG_AI_OLLAMA_INVALID,
        CFG_TELEGRAM_TOKEN_INVALID,
        CFG_TELEGRAM_IDENTITY_INVALID,
        CFG_TELEGRAM_OWNER_INVALID,
        CFG_TELEGRAM_OPTION_INVALID,
    }
)

_SECRET_PLACEHOLDER_PATTERNS = (
    re.compile(r"CHANGE[_-]?ME", re.IGNORECASE),
    re.compile(r"YOUR[_-]?(?:SECRET|TOKEN|KEY)", re.IGNORECASE),
    re.compile(r"REPLACE[_-]?(?:WITH|ME)", re.IGNORECASE),
    re.compile(r"EXAMPLE", re.IGNORECASE),
    re.compile(r"PLACEHOLDER", re.IGNORECASE),
    re.compile(r"DUMMY", re.IGNORECASE),
)
_TELEGRAM_TOKEN = re.compile(
    r"^[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,128}$"
)
_CANONICAL_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")


class ConfigurationError(ValueError):
    """Configuration failure whose message is always an allowlisted safe code."""

    def __init__(self, code: str):
        safe_code = code if code in SAFE_CONFIGURATION_CODES else CFG_ENV_INVALID
        self.code = safe_code
        super().__init__(safe_code)


def has_control_characters(value: str) -> bool:
    """Reject every Unicode Other (C*) category.

    This includes C0/C1 controls, NUL, bidi/zero-width format characters,
    surrogates, private-use code points, and unassigned characters. Normal
    Unicode letters, marks, numbers, punctuation, separators, and symbols
    remain available to human-readable labels.
    """
    return any(unicodedata.category(character).startswith("C") for character in value)


def is_placeholder(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PLACEHOLDER_PATTERNS)


def is_trivially_repeated(value: str) -> bool:
    """Detect full-string repetition without claiming entropy measurement."""
    length = len(value)
    for unit_length in range(1, length // 2 + 1):
        if length % unit_length:
            continue
        unit = value[:unit_length]
        if unit * (length // unit_length) == value:
            return True
    return False


def validate_app_environment(value) -> str:
    if not isinstance(value, str) or value not in VALID_APP_ENVIRONMENTS:
        raise ConfigurationError(CFG_ENV_INVALID)
    return value


def validate_secret(
    value,
    *,
    code: str,
    minimum_length: int = MINIMUM_SECRET_LENGTH,
    maximum_length: int = MAXIMUM_SECRET_LENGTH,
) -> str:
    if (
        not isinstance(value, str)
        or not minimum_length <= len(value) <= maximum_length
        or value != value.strip()
        or not value
        or has_control_characters(value)
        or is_placeholder(value)
        or is_trivially_repeated(value)
    ):
        raise ConfigurationError(code)
    return value


def validate_visible_label(value, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_CONFIGURATION_LABEL_LENGTH
        or value != value.strip()
        or has_control_characters(value)
    ):
        raise ConfigurationError(code)
    return value


def validate_deployed_identity_label(
    value,
    *,
    app_env: str,
    development_value: str,
    code: str,
) -> str:
    validated = validate_visible_label(value, code=code)
    if app_env in DEPLOYED_APP_ENVIRONMENTS and validated == development_value:
        raise ConfigurationError(code)
    return validated


def parse_strict_positive_integer(value, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _CANONICAL_POSITIVE_INTEGER.fullmatch(value):
        parsed = int(value)
    else:
        raise ConfigurationError(code)
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(code)
    return parsed


def parse_strict_boolean(value, *, code: str) -> bool:
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConfigurationError(code)


def validate_ai_provider(value) -> str:
    if value not in {"ollama", "openai"}:
        raise ConfigurationError(CFG_AI_PROVIDER_INVALID)
    return value


def validate_model_name(value, *, code: str) -> str:
    return validate_visible_label(value, code=code)


def validate_openai_api_key(value) -> str:
    return validate_secret(
        value,
        code=CFG_AI_OPENAI_INVALID,
        minimum_length=20,
        maximum_length=MAXIMUM_SECRET_LENGTH,
    )


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_ollama_url(value, *, app_env: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_PROVIDER_URL_LENGTH
        or value != value.strip()
        or has_control_characters(value)
    ):
        raise ConfigurationError(CFG_AI_OLLAMA_INVALID)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigurationError(CFG_AI_OLLAMA_INVALID) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ConfigurationError(CFG_AI_OLLAMA_INVALID)
    if (
        app_env in DEPLOYED_APP_ENVIRONMENTS
        and parsed.scheme == "http"
        and not _is_loopback_hostname(hostname)
    ):
        raise ConfigurationError(CFG_AI_OLLAMA_INVALID)
    return value


def validate_telegram_bot_token(value) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_TELEGRAM_TOKEN_LENGTH
        or value != value.strip()
        or has_control_characters(value)
        or is_placeholder(value)
        or not _TELEGRAM_TOKEN.fullmatch(value)
    ):
        raise ConfigurationError(CFG_TELEGRAM_TOKEN_INVALID)
    return value
