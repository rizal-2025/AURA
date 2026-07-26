"""Read-only safety preflight for the local Telegram UAT runner."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_ai_settings, get_database_settings
from app.db.models.conversation_workflow_state import ConversationWorkflowState
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.db.models.telegram_identity import TelegramIdentity
from app.integrations.telegram.runner import (
    TelegramRunnerSettings,
    validate_runner_configuration,
)


ALLOWED_DATABASE = "aura_telegram_uat"
REQUIRED_OLLAMA_MODEL = "qwen2.5:3b"
REQUIRED_TABLE_MODELS = (
    Customer,
    Reservation,
    TelegramIdentity,
    SupportTicket,
    SupportTicketNotification,
    ConversationWorkflowState,
)

Output = Callable[[str], None]


def _emit(output: Output, level: str, message: str) -> None:
    output(f"{level}: {message}")


def _safe_current_user(value: Any) -> str:
    """Render the required database user without allowing control characters."""
    if not isinstance(value, str) or not value:
        return "<unavailable>"
    rendered = "".join(
        character if character.isprintable() and character not in "\r\n\t" else "?"
        for character in value
    )
    return rendered[:128] or "<unavailable>"


def required_table_names() -> tuple[str, ...]:
    """Return names declared by the current models, not duplicated guesses."""
    return tuple(model.__table__.name for model in REQUIRED_TABLE_MODELS)


def check_database(
    *,
    output: Output = print,
    settings_loader: Callable[[], Any] = get_database_settings,
    engine_factory: Callable[..., Any] = create_engine,
    inspector_factory: Callable[[Any], Any] = inspect,
) -> bool:
    try:
        database_settings = settings_loader()
        database_url = getattr(database_settings, "DATABASE_URL", None)
    except Exception:
        _emit(
            output,
            "FAIL",
            "DATABASE_URL is missing or invalid in the AURA database configuration.",
        )
        return False

    if not isinstance(database_url, str) or not database_url.strip():
        _emit(output, "FAIL", "DATABASE_URL is not configured.")
        return False

    _emit(output, "PASS", "DATABASE_URL is configured (value hidden).")

    engine = None
    try:
        engine = engine_factory(
            database_url,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
            if len(row) != 2:
                _emit(output, "FAIL", "Database identity query returned an invalid result.")
                return False

            database_name, current_user = row
            _emit(
                output,
                "PASS",
                f"Database current_user: {_safe_current_user(current_user)}",
            )
            if database_name != ALLOWED_DATABASE:
                _emit(
                    output,
                    "FAIL",
                    "Database rejected. Manual Telegram UAT requires exactly "
                    f"{ALLOWED_DATABASE}.",
                )
                return False

            _emit(
                output,
                "PASS",
                f"current_database() is exactly {ALLOWED_DATABASE}.",
            )

            available_tables = set(
                inspector_factory(connection).get_table_names()
            )
            missing_tables = [
                name for name in required_table_names() if name not in available_tables
            ]
            if missing_tables:
                for table_name in missing_tables:
                    _emit(
                        output,
                        "FAIL",
                        f"Required AURA table is missing: {table_name}.",
                    )
                return False

            for table_name in required_table_names():
                _emit(output, "PASS", f"Required AURA table exists: {table_name}.")
            return True
    except Exception:
        _emit(
            output,
            "FAIL",
            "Database connection or read-only inspection failed safely.",
        )
        return False
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _fetch_ollama_tags(base_url: str) -> Any:
    parsed = urlsplit(base_url)
    tags_url = urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    with httpx.Client(
        timeout=5.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = client.get(tags_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()


def _ollama_model_names(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    models = payload.get("models")
    if not isinstance(models, list):
        return set()
    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        for key in ("name", "model"):
            value = model.get(key)
            if isinstance(value, str):
                names.add(value)
    return names


def check_ollama(
    *,
    output: Output = print,
    settings_loader: Callable[[], Any] = get_ai_settings,
    tags_fetcher: Callable[[str], Any] = _fetch_ollama_tags,
) -> bool:
    try:
        ai_settings = settings_loader()
        provider = getattr(ai_settings, "AI_PROVIDER", None)
        base_url = getattr(ai_settings, "OLLAMA_BASE_URL", None)
        configured_model = getattr(ai_settings, "OLLAMA_MODEL", None)
    except Exception:
        _emit(output, "FAIL", "AURA Ollama configuration is missing or invalid.")
        return False

    if provider != "ollama":
        _emit(output, "FAIL", "AI_PROVIDER must be ollama for Telegram UAT.")
        return False
    if configured_model != REQUIRED_OLLAMA_MODEL:
        _emit(
            output,
            "FAIL",
            f"OLLAMA_MODEL must be exactly {REQUIRED_OLLAMA_MODEL}.",
        )
        return False
    if not isinstance(base_url, str) or not base_url:
        _emit(output, "FAIL", "OLLAMA_BASE_URL is not configured.")
        return False

    try:
        payload = tags_fetcher(base_url)
    except Exception:
        _emit(output, "FAIL", "Ollama is unavailable or returned an invalid response.")
        return False

    _emit(output, "PASS", "Ollama is reachable.")
    if REQUIRED_OLLAMA_MODEL not in _ollama_model_names(payload):
        _emit(
            output,
            "FAIL",
            f"Required Ollama model is unavailable: {REQUIRED_OLLAMA_MODEL}.",
        )
        return False

    _emit(
        output,
        "PASS",
        f"Required Ollama model is available: {REQUIRED_OLLAMA_MODEL}.",
    )
    return True


def check_telegram_configuration(
    *,
    output: Output = print,
    settings_loader: Callable[[], Any] = TelegramRunnerSettings,
    validator: Callable[[Any], Any] = validate_runner_configuration,
) -> bool:
    try:
        runner_settings = settings_loader()
        validator(runner_settings)
    except Exception:
        _emit(
            output,
            "FAIL",
            "Required Telegram configuration is missing or invalid.",
        )
        return False

    _emit(
        output,
        "PASS",
        "Required Telegram configuration exists and is valid (values hidden).",
    )
    return True


def run_preflight(
    *,
    output: Output = print,
    database_settings_loader: Callable[[], Any] = get_database_settings,
    engine_factory: Callable[..., Any] = create_engine,
    inspector_factory: Callable[[Any], Any] = inspect,
    ai_settings_loader: Callable[[], Any] = get_ai_settings,
    ollama_tags_fetcher: Callable[[str], Any] = _fetch_ollama_tags,
    telegram_settings_loader: Callable[[], Any] = TelegramRunnerSettings,
    telegram_validator: Callable[[Any], Any] = validate_runner_configuration,
) -> int:
    _emit(output, "WARNING", "Telegram UAT preflight is read-only.")

    results = [
        check_database(
            output=output,
            settings_loader=database_settings_loader,
            engine_factory=engine_factory,
            inspector_factory=inspector_factory,
        ),
        check_ollama(
            output=output,
            settings_loader=ai_settings_loader,
            tags_fetcher=ollama_tags_fetcher,
        ),
        check_telegram_configuration(
            output=output,
            settings_loader=telegram_settings_loader,
            validator=telegram_validator,
        ),
    ]

    _emit(
        output,
        "WARNING",
        "No Telegram API call was made; credentials were validated locally.",
    )
    if all(results):
        _emit(output, "PASS", "All mandatory Telegram UAT checks passed.")
        return 0

    _emit(output, "FAIL", "Telegram UAT preflight failed. The bot must not start.")
    return 1


def main() -> int:
    try:
        return run_preflight()
    except Exception:
        print("FAIL: Telegram UAT preflight stopped safely.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
