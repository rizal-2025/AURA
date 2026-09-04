"""Bounded, concurrent-writer-safe provider runtime event persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
from uuid import UUID


EVENT_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
LOCK_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH"
_EVENT_TYPES = frozenset(
    {
        "AI_PROVIDER_ATTEMPT",
        "AI_PROVIDER_OUTCOME",
        "AI_PROVIDER_FALLBACK",
    }
)
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_OUTCOMES = frozenset(
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
_LOCK_TIMEOUT_SECONDS = 5.0
_MAX_RECORD_BYTES = 2048


def parse_provider_runtime_event(message: str) -> dict[str, object] | None:
    """Return an exact safe event schema, or reject the record."""
    try:
        event = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("event") not in _EVENT_TYPES:
        return None

    common_fields = {
        "event",
        "model",
        "operation",
        "provider",
        "request_id",
    }
    event_type = event["event"]
    expected_fields = {
        "AI_PROVIDER_ATTEMPT": common_fields,
        "AI_PROVIDER_OUTCOME": common_fields | {"elapsed_ms", "outcome"},
        "AI_PROVIDER_FALLBACK": common_fields | {"locale", "reason"},
    }[event_type]
    if set(event) != expected_fields:
        return None

    request_id = event.get("request_id")
    try:
        canonical_request_id = str(UUID(str(request_id)))
    except (AttributeError, TypeError, ValueError):
        return None
    if request_id != canonical_request_id:
        return None
    if (
        event.get("provider") != "openai"
        or event.get("operation") != "responses.create"
        or not isinstance(event.get("model"), str)
        or _MODEL_PATTERN.fullmatch(event["model"]) is None
    ):
        return None

    if event_type == "AI_PROVIDER_OUTCOME":
        elapsed_ms = event.get("elapsed_ms")
        if (
            isinstance(elapsed_ms, bool)
            or not isinstance(elapsed_ms, int)
            or elapsed_ms < 0
            or elapsed_ms > 3_600_000
            or event.get("outcome") not in _OUTCOMES
        ):
            return None
    elif event_type == "AI_PROVIDER_FALLBACK" and (
        event.get("locale") not in {"en-US", "id-ID"}
        or event.get("reason") not in (_OUTCOMES - {"SUCCESS"})
    ):
        return None
    return event


class ProviderRuntimeEventFilter(logging.Filter):
    """Accept only bounded, request-scoped OpenAI lifecycle events."""

    def filter(self, record: logging.LogRecord) -> bool:
        event = parse_provider_runtime_event(record.getMessage())
        if event is None:
            return False
        record.aura_provider_runtime_event = event
        return True


class ProviderRuntimeEventFormatter(logging.Formatter):
    """Render one deterministic JSON record with an ordered UTC timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        event = dict(record.aura_provider_runtime_event)
        event["timestamp"] = datetime.fromtimestamp(
            record.created,
            tz=timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return json.dumps(event, separators=(",", ":"), sort_keys=True)


class ProviderRuntimeEventFileHandler(logging.Handler):
    """Append complete records while holding a Windows inter-process lock."""

    def __init__(self, event_path: Path, lock_path: Path) -> None:
        super().__init__()
        self.baseFilename = str(event_path)
        self.lockFilename = str(lock_path)

    @staticmethod
    def _acquire_lock(lock_file) -> None:
        if os.name != "nt":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return

        import msvcrt

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_LOCK_TIMEOUT")
                time.sleep(0.01)

    @staticmethod
    def _release_lock(lock_file) -> None:
        if os.name != "nt":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return

        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = (self.format(record) + "\n").encode("utf-8")
            if len(payload) > _MAX_RECORD_BYTES:
                raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_RECORD_TOO_LARGE")
            with open(self.lockFilename, "r+b", buffering=0) as lock_file:
                self._acquire_lock(lock_file)
                try:
                    with open(self.baseFilename, "ab", buffering=0) as event_file:
                        written = event_file.write(payload)
                        if written != len(payload):
                            raise OSError("provider runtime event short write")
                        os.fsync(event_file.fileno())
                finally:
                    self._release_lock(lock_file)
        except Exception:
            # The provider call remains semantics-neutral. A missing record is an
            # explicit fail-closed condition for the rollout consumer.
            self.handleError(record)


def _validated_file_path(value: str | None, error_code: str) -> Path:
    if value is None or value == "" or value != value.strip():
        raise RuntimeError(error_code)
    path = Path(value)
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or not path.is_file()
        or path.is_symlink()
    ):
        raise RuntimeError(error_code)
    return path


def configure_provider_runtime_event_logging(logger: logging.Logger) -> None:
    """Attach exactly one protected event-only sink when startup configures it."""
    event_value = os.getenv(EVENT_PATH_ENV)
    lock_value = os.getenv(LOCK_PATH_ENV)
    if event_value is None and lock_value is None:
        return
    event_path = _validated_file_path(
        event_value,
        "AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID",
    )
    lock_path = _validated_file_path(
        lock_value,
        "AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH_INVALID",
    )
    if (
        event_path.parent != lock_path.parent
        or event_path == lock_path
        or lock_path.stat().st_size < 1
    ):
        raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH_INVALID")

    expected_event = os.path.normcase(os.path.abspath(event_path))
    expected_lock = os.path.normcase(os.path.abspath(lock_path))
    existing = [
        handler
        for handler in logger.handlers
        if isinstance(handler, ProviderRuntimeEventFileHandler)
    ]
    if len(existing) > 1:
        raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_HANDLER_DUPLICATE")
    if existing:
        current_event = os.path.normcase(os.path.abspath(existing[0].baseFilename))
        current_lock = os.path.normcase(os.path.abspath(existing[0].lockFilename))
        if current_event != expected_event or current_lock != expected_lock:
            raise RuntimeError("AURA_PROVIDER_RUNTIME_EVENT_HANDLER_CONFLICT")
        return

    handler = ProviderRuntimeEventFileHandler(event_path, lock_path)
    handler.addFilter(ProviderRuntimeEventFilter())
    handler.setFormatter(ProviderRuntimeEventFormatter())
    logger.addHandler(handler)
