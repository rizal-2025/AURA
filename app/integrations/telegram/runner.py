"""Separate local long-polling entry point: ``python -m app.integrations.telegram.runner``."""

import asyncio
import re
from dataclasses import dataclass, field

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS,
    DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS,
    MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH,
    get_ai_settings,
    get_database_settings,
    get_environment_settings,
)
from app.core.config_validation import (
    CFG_AI_OLLAMA_INVALID,
    CFG_AI_OPENAI_INVALID,
    CFG_AI_PROVIDER_INVALID,
    CFG_DATABASE_INVALID,
    CFG_ENV_INVALID,
    CFG_TELEGRAM_IDENTITY_INVALID,
    CFG_TELEGRAM_OPTION_INVALID,
    CFG_TELEGRAM_OWNER_INVALID,
    CFG_TELEGRAM_TOKEN_INVALID,
    ConfigurationError,
    parse_strict_boolean,
    parse_strict_positive_integer,
    validate_app_environment,
    validate_secret,
    validate_telegram_bot_token,
)
from app.core.logger import configure_safe_logging, logger
from app.integrations.telegram.owner_notification_dispatcher import (
    OwnerNotificationDispatcher,
)


class TelegramRunnerConfigurationError(RuntimeError):
    """Configuration error whose text never includes a secret or token."""

    SAFE_CODES = {
        CFG_ENV_INVALID,
        CFG_DATABASE_INVALID,
        CFG_AI_PROVIDER_INVALID,
        CFG_AI_OPENAI_INVALID,
        CFG_AI_OLLAMA_INVALID,
        CFG_TELEGRAM_TOKEN_INVALID,
        CFG_TELEGRAM_IDENTITY_INVALID,
        CFG_TELEGRAM_OWNER_INVALID,
        CFG_TELEGRAM_OPTION_INVALID,
    }

    def __init__(self, code: str):
        self.code = code if code in self.SAFE_CODES else CFG_TELEGRAM_OPTION_INVALID
        super().__init__(self.code)


class TelegramWebhookConflictError(RuntimeError):
    """Refuse polling while a webhook remains active unless explicitly cleared."""


class TelegramRunnerSettings(BaseSettings):
    """Runner-only raw environment; validation happens immediately afterward."""

    APP_ENV: object = None
    TELEGRAM_BOT_TOKEN: object = Field(default=None, repr=False)
    TELEGRAM_IDENTITY_SECRET: object = Field(default=None, repr=False)
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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )


TelegramRunnerEnvironment = TelegramRunnerSettings


@dataclass(frozen=True)
class TelegramRunnerConfiguration:
    bot_token: str = field(repr=False)
    identity_secret: str = field(repr=False)
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
    config = config or TelegramRunnerSettings()
    try:
        validate_app_environment(getattr(config, "APP_ENV", None))
    except ConfigurationError as error:
        raise TelegramRunnerConfigurationError(error.code) from None

    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    secret = getattr(config, "TELEGRAM_IDENTITY_SECRET", None)
    timeout = getattr(config, "TELEGRAM_POLL_TIMEOUT_SECONDS", DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS)
    try:
        validated_token = validate_telegram_bot_token(token)
        validated_secret = validate_secret(
            secret,
            code=CFG_TELEGRAM_IDENTITY_INVALID,
            minimum_length=MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH,
        )
        parsed_timeout = parse_strict_positive_integer(
            timeout,
            minimum=1,
            maximum=60,
            code=CFG_TELEGRAM_OPTION_INVALID,
        )
    except ConfigurationError as error:
        raise TelegramRunnerConfigurationError(error.code) from None

    def strict_boolean(name: str, default: bool) -> bool:
        value = getattr(config, name, default)
        try:
            return parse_strict_boolean(value, code=CFG_TELEGRAM_OPTION_INVALID)
        except ConfigurationError as error:
            raise TelegramRunnerConfigurationError(error.code) from None

    def strict_integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = getattr(config, name, default)
        try:
            return parse_strict_positive_integer(
                value,
                minimum=minimum,
                maximum=maximum,
                code=(
                    CFG_TELEGRAM_OWNER_INVALID
                    if name == "TELEGRAM_OWNER_CHAT_ID"
                    else CFG_TELEGRAM_OPTION_INVALID
                ),
            )
        except ConfigurationError as error:
            raise TelegramRunnerConfigurationError(error.code) from None

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
        bot_token=validated_token,
        identity_secret=validated_secret,
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
    # Validate every non-Telegram runner dependency before constructing PTB.
    get_environment_settings()
    get_database_settings()
    get_ai_settings()
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    except ImportError as error:
        raise TelegramRunnerConfigurationError(
            "Telegram dependency is unavailable. Install the project dependencies."
        ) from error

    from app.db.database import SessionLocal
    from app.integrations.telegram.handlers import TelegramCustomerHandlers
    from app.integrations.telegram.owner_command_handlers import (
        TelegramOwnerCommandHandlers,
        unknown_command,
    )

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
        runner_environment = TelegramRunnerSettings()
        runner_config = validate_runner_configuration(runner_environment)
        get_environment_settings()
        get_database_settings()
        get_ai_settings()
        application = build_application(runner_environment)
        application.run_polling(
            allowed_updates=["message"],
            drop_pending_updates=runner_config.drop_pending_updates,
            timeout=runner_config.poll_timeout_seconds,
        )
    except TelegramRunnerConfigurationError as error:
        logger.error(
            "TELEGRAM RUNNER: status=failed category=configuration_error code=%s",
            error.code,
        )
        raise SystemExit("Telegram runner failed to start safely.") from None
    except ConfigurationError as error:
        logger.error(
            "TELEGRAM RUNNER: status=failed category=configuration_error code=%s",
            error.code,
        )
        raise SystemExit("Telegram runner failed to start safely.") from None
    except TelegramWebhookConflictError:
        logger.error(
            "TELEGRAM RUNNER: status=failed category=webhook_conflict"
        )
        raise SystemExit("Telegram runner failed to start safely.") from None
    except Exception as error:
        logger.error("TELEGRAM RUNNER: status=failed category=%s", type(error).__name__)
        raise SystemExit("Telegram runner failed to start safely.") from None


if __name__ == "__main__":
    main()
