"""Separate local long-polling entry point: ``python -m app.integrations.telegram.runner``."""

import asyncio
import re
from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS,
    MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH,
)
from app.core.logger import configure_safe_logging, logger
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.owner_command_handlers import (
    TelegramOwnerCommandHandlers,
    unknown_command,
)
from app.integrations.telegram.owner_notification_dispatcher import OwnerNotificationDispatcher
from app.db.database import SessionLocal


class TelegramRunnerConfigurationError(RuntimeError):
    """Configuration error whose text never includes a secret or token."""


class TelegramWebhookConflictError(RuntimeError):
    """Refuse polling while a webhook remains active unless explicitly cleared."""


class TelegramRunnerEnvironment(BaseSettings):
    """Runner-only raw environment; validation happens immediately afterward."""

    TELEGRAM_BOT_TOKEN: object = None
    TELEGRAM_IDENTITY_SECRET: object = None
    TELEGRAM_CLEAR_WEBHOOK_ON_START: object = False
    TELEGRAM_DROP_PENDING_UPDATES: object = False
    TELEGRAM_POLL_TIMEOUT_SECONDS: object = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    TELEGRAM_OWNER_NOTIFICATIONS_ENABLED: object = False
    TELEGRAM_OWNER_COMMANDS_ENABLED: object = False
    TELEGRAM_OWNER_CHAT_ID: object = None
    TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS
    TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS
    TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS
    TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@dataclass(frozen=True)
class TelegramRunnerConfiguration:
    bot_token: str
    identity_secret: str
    clear_webhook_on_start: bool
    drop_pending_updates: bool
    poll_timeout_seconds: int
    owner_notifications_enabled: bool
    owner_commands_enabled: bool
    owner_chat_id: int | None
    owner_notification_poll_seconds: int
    owner_notification_max_attempts: int
    owner_notification_retry_base_seconds: int
    owner_notification_lease_seconds: int


def validate_runner_configuration(config=None) -> TelegramRunnerConfiguration:
    config = config or TelegramRunnerEnvironment()
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    secret = getattr(config, "TELEGRAM_IDENTITY_SECRET", None)
    timeout = getattr(config, "TELEGRAM_POLL_TIMEOUT_SECONDS", DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS)
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token.strip()):
        raise TelegramRunnerConfigurationError("Telegram bot token is missing or invalid.")
    if (
        not isinstance(secret, str)
        or len(secret) < MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH
        or not secret.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in secret)
    ):
        raise TelegramRunnerConfigurationError("Telegram identity configuration is missing or invalid.")

    if isinstance(timeout, bool):
        parsed_timeout = None
    elif isinstance(timeout, int):
        parsed_timeout = timeout
    elif isinstance(timeout, str) and re.fullmatch(r"[1-9][0-9]*", timeout):
        parsed_timeout = int(timeout)
    else:
        parsed_timeout = None
    if parsed_timeout is None or not 1 <= parsed_timeout <= 60:
        raise TelegramRunnerConfigurationError("Telegram polling timeout is invalid.")

    def strict_boolean(name: str, default: bool) -> bool:
        value = getattr(config, name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise TelegramRunnerConfigurationError("Telegram boolean configuration is invalid.")

    def strict_integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = getattr(config, name, default)
        if isinstance(value, bool):
            parsed = None
        elif isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            parsed = int(value)
        else:
            parsed = None
        if parsed is None or not minimum <= parsed <= maximum:
            raise TelegramRunnerConfigurationError("Telegram owner integer configuration is invalid.")
        return parsed

    owner_enabled = strict_boolean("TELEGRAM_OWNER_NOTIFICATIONS_ENABLED", False)
    owner_commands_enabled = strict_boolean("TELEGRAM_OWNER_COMMANDS_ENABLED", False)
    owner_chat_id = None
    if owner_enabled:
        owner_chat_id = strict_integer(
            "TELEGRAM_OWNER_CHAT_ID", 0, 1, 9_223_372_036_854_775_807
        )
    if owner_commands_enabled:
        # Validate independently so owner commands do not depend on the Phase E
        # notification feature being enabled.
        owner_chat_id = strict_integer(
            "TELEGRAM_OWNER_CHAT_ID", 0, 1, 9_223_372_036_854_775_807
        )

    return TelegramRunnerConfiguration(
        bot_token=token.strip(),
        identity_secret=secret,
        clear_webhook_on_start=strict_boolean("TELEGRAM_CLEAR_WEBHOOK_ON_START", False),
        drop_pending_updates=strict_boolean("TELEGRAM_DROP_PENDING_UPDATES", False),
        poll_timeout_seconds=parsed_timeout,
        owner_notifications_enabled=owner_enabled,
        owner_commands_enabled=owner_commands_enabled,
        owner_chat_id=owner_chat_id,
        owner_notification_poll_seconds=strict_integer(
            "TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS",
            DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS,
            1,
            300,
        ),
        owner_notification_max_attempts=strict_integer(
            "TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS",
            DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS,
            1,
            20,
        ),
        owner_notification_retry_base_seconds=strict_integer(
            "TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS",
            DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS,
            1,
            3600,
        ),
        owner_notification_lease_seconds=strict_integer(
            "TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS",
            DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS,
            5,
            3600,
        ),
    )


async def prepare_polling(application) -> None:
    """Check webhook state without emitting its URL or other remote metadata."""
    config: TelegramRunnerConfiguration = application.bot_data["aura_runner_config"]
    webhook_info = await application.bot.get_webhook_info()
    if getattr(webhook_info, "url", None):
        if not config.clear_webhook_on_start:
            raise TelegramWebhookConflictError(
                "An active Telegram webhook must be cleared explicitly before polling."
            )
        await application.bot.delete_webhook(
            drop_pending_updates=config.drop_pending_updates,
        )
    if config.owner_notifications_enabled:
        if application.bot_data.get("aura_owner_notification_task") is not None:
            return
        dispatcher = OwnerNotificationDispatcher(
            bot=application.bot,
            session_factory=application.bot_data["aura_session_factory"],
            owner_chat_id=config.owner_chat_id,
            config=config,
        )
        application.bot_data["aura_owner_notification_dispatcher"] = dispatcher
        application.bot_data["aura_owner_notification_task"] = asyncio.create_task(
            dispatcher.run(), name="aura-owner-notification-dispatcher"
        )


async def shutdown_owner_notifications(application) -> None:
    dispatcher = application.bot_data.get("aura_owner_notification_dispatcher")
    task = application.bot_data.get("aura_owner_notification_task")
    if dispatcher is not None:
        dispatcher.stop()
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    application.bot_data.pop("aura_owner_notification_task", None)
    application.bot_data.pop("aura_owner_notification_dispatcher", None)


def build_application(config=None, **handler_dependencies):
    configure_safe_logging()
    runner_config = validate_runner_configuration(config)
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    except ImportError as error:
        raise TelegramRunnerConfigurationError(
            "Telegram dependency is unavailable. Install the project dependencies."
        ) from error

    owner_ticket_service = handler_dependencies.pop("owner_ticket_service", None)
    handlers = TelegramCustomerHandlers(
        identity_secret=runner_config.identity_secret,
        **handler_dependencies,
    )
    application = (
        ApplicationBuilder()
        .token(runner_config.bot_token)
        .concurrent_updates(False)
        .post_init(prepare_polling)
        .post_shutdown(shutdown_owner_notifications)
        .build()
    )
    application.bot_data["aura_runner_config"] = runner_config
    application.bot_data["aura_session_factory"] = handler_dependencies.get("session_factory", SessionLocal)
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("status", handlers.status))
    if runner_config.owner_commands_enabled:
        owner_handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=runner_config.owner_chat_id,
            session_factory=handler_dependencies.get("session_factory", SessionLocal),
            ticket_service=owner_ticket_service,
        )
        application.add_handler(CommandHandler("tickets", owner_handlers.tickets))
        application.add_handler(CommandHandler("ticket", owner_handlers.ticket))
        application.add_handler(CommandHandler("take", owner_handlers.take))
        application.add_handler(CommandHandler("resolve", owner_handlers.resolve))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_message))
    application.add_handler(MessageHandler(filters.ALL, handlers.non_text_message))
    application.add_error_handler(safe_ptb_error_handler)
    return application


async def safe_ptb_error_handler(update, context) -> None:
    """Record only an allowlisted failure category and exception class."""
    error = getattr(context, "error", None)
    exception_name = re.sub(
        r"[^A-Za-z0-9_]",
        "",
        type(error).__name__ if error is not None else "UnknownError",
    ) or "UnknownError"
    logger.error(
        "TELEGRAM FAILURE: category=ptb_update_error exception=%s identifier=PTB-UPDATE",
        exception_name,
    )


def main() -> None:
    try:
        runner_environment = TelegramRunnerEnvironment()
        runner_config = validate_runner_configuration(runner_environment)
        application = build_application(runner_environment)
        application.run_polling(
            allowed_updates=["message"],
            drop_pending_updates=runner_config.drop_pending_updates,
            timeout=runner_config.poll_timeout_seconds,
        )
    except (TelegramRunnerConfigurationError, TelegramWebhookConflictError) as error:
        logger.error("TELEGRAM RUNNER: status=failed category=%s", type(error).__name__)
        raise SystemExit("Telegram runner failed to start safely.") from None
    except Exception as error:
        logger.error("TELEGRAM RUNNER: status=failed category=%s", type(error).__name__)
        raise SystemExit("Telegram runner failed to start safely.") from None


if __name__ == "__main__":
    main()
