from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from uuid import UUID


_BOT_URL_PATTERN = re.compile(r"/bot[^/\s]+/", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_DATABASE_PASSWORD_PATTERN = re.compile(
    r"(?P<prefix>[a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@",
    re.IGNORECASE,
)
_PROVIDER_RUNTIME_EVENT_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
_PROVIDER_RUNTIME_EVENTS = frozenset(
    {
        "AI_PROVIDER_ATTEMPT",
        "AI_PROVIDER_OUTCOME",
        "AI_PROVIDER_FALLBACK",
    }
)
_PROVIDER_RUNTIME_MODELS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROVIDER_RUNTIME_OUTCOMES = frozenset(
    {
        "AUTH",
        "BILLING",
        "CLIENT_ERROR",
        "PROVIDER_ERROR",
        "RATE_LIMIT",
        "SUCCESS",
        "TIMEOUT",
        "UNKNOWN_ERROR",
    }
)


def redact_sensitive_text(value) -> str:
    """Redact common credentials without returning their original values."""
    text = str(value)
    text = _BOT_URL_PATTERN.sub("/bot[REDACTED]/", text)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _DATABASE_PASSWORD_PATTERN.sub(r"\g<prefix>[REDACTED]@", text)
    for environment_name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_IDENTITY_SECRET",
        "TELEGRAM_OWNER_CHAT_ID",
        "AUTH_JWT_SECRET",
        "OPENAI_API_KEY",
        "DEMO_BFF_SERVICE_TOKEN",
    ):
        secret = os.getenv(environment_name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class SensitiveDataFilter(logging.Filter):
    """Sanitize log messages before any configured handler receives them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Second-layer redaction that also covers formatted exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


def _parse_provider_runtime_event(message: str) -> dict[str, object] | None:
    try:
        event = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(event, dict)
        or event.get("event") not in _PROVIDER_RUNTIME_EVENTS
    ):
        return None

    common_fields = {
        "event",
        "model",
        "operation",
        "provider",
        "request_id",
    }
    event_type = event["event"]
    event_fields = {
        "AI_PROVIDER_ATTEMPT": common_fields,
        "AI_PROVIDER_OUTCOME": common_fields | {"elapsed_ms", "outcome"},
        "AI_PROVIDER_FALLBACK": common_fields | {"locale", "reason"},
    }[event_type]
    if set(event) != event_fields:
        return None

    request_id = event.get("request_id")
    try:
        canonical_request_id = str(UUID(request_id))
    except (AttributeError, TypeError, ValueError):
        return None
    if request_id != canonical_request_id:
        return None
    if (
        event.get("provider") != "openai"
        or event.get("operation") != "responses.create"
        or not isinstance(event.get("model"), str)
        or _PROVIDER_RUNTIME_MODELS.fullmatch(event["model"]) is None
    ):
        return None

    if event_type == "AI_PROVIDER_OUTCOME":
        elapsed_ms = event.get("elapsed_ms")
        if (
            isinstance(elapsed_ms, bool)
            or not isinstance(elapsed_ms, int)
            or elapsed_ms < 0
            or elapsed_ms > 3_600_000
            or event.get("outcome") not in _PROVIDER_RUNTIME_OUTCOMES
        ):
            return None
    elif event_type == "AI_PROVIDER_FALLBACK":
        if (
            event.get("locale") not in {"en-US", "id-ID"}
            or event.get("reason")
            not in (_PROVIDER_RUNTIME_OUTCOMES - {"SUCCESS"})
        ):
            return None
    return event


class ProviderRuntimeEventFilter(logging.Filter):
    """Accept only the bounded, request-scoped OpenAI event schema."""

    def filter(self, record: logging.LogRecord) -> bool:
        event = _parse_provider_runtime_event(record.getMessage())
        if event is None:
            return False
        record.aura_provider_runtime_event = event
        return True


class ProviderRuntimeEventFormatter(logging.Formatter):
    """Persist one deterministic JSON record per accepted provider event."""

    def format(self, record: logging.LogRecord) -> str:
        event = dict(record.aura_provider_runtime_event)
        event["timestamp"] = datetime.fromtimestamp(
            record.created,
            tz=timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return json.dumps(event, separators=(",", ":"), sort_keys=True)


class ProviderRuntimeEventFileHandler(logging.FileHandler):
    """Marker type used to enforce exactly one durable provider event sink."""


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
)

logger = logging.getLogger("AURA")


def configure_provider_runtime_event_logging() -> None:
    """Attach the protected event-only sink configured by the Windows launcher."""
    configured_path = os.getenv(_PROVIDER_RUNTIME_EVENT_PATH_ENV)
    if configured_path is None:
        return
    if configured_path == "" or configured_path != configured_path.strip():
        raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID")

    path = Path(configured_path)
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or not path.is_file()
        or path.is_symlink()
    ):
        raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID")

    expected = os.path.normcase(os.path.abspath(path))
    existing = [
        handler
        for handler in logger.handlers
        if isinstance(handler, ProviderRuntimeEventFileHandler)
    ]
    if len(existing) > 1:
        raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_HANDLER_DUPLICATE")
    if existing:
        current = os.path.normcase(os.path.abspath(existing[0].baseFilename))
        if current != expected:
            raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_HANDLER_CONFLICT")
        return

    handler = ProviderRuntimeEventFileHandler(
        path,
        mode="a",
        encoding="utf-8",
        delay=False,
    )
    handler.addFilter(ProviderRuntimeEventFilter())
    handler.setFormatter(ProviderRuntimeEventFormatter())
    logger.addHandler(handler)


def configure_safe_logging() -> None:
    """Keep AURA logs useful while suppressing credential-bearing client logs."""
    redaction_filter = SensitiveDataFilter()
    for logger_name in (
        "AURA",
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "telegram.request",
        "telegram.ext.Application",
        "telegram.ext.Updater",
    ):
        target_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(item, SensitiveDataFilter)
            for item in target_logger.filters
        ):
            target_logger.addFilter(redaction_filter)

    for logger_name in (
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "telegram.request",
        "telegram.ext.Application",
        "telegram.ext.Updater",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))

    configure_provider_runtime_event_logging()


configure_safe_logging()
