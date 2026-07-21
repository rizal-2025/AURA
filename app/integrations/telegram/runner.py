"""Separate local long-polling entry point: ``python -m app.integrations.telegram.runner``."""

import re
from dataclasses import dataclass

from app.core.config import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH,
    settings,
)
from app.core.logger import configure_safe_logging, logger
from app.integrations.telegram.handlers import TelegramCustomerHandlers


class TelegramRunnerConfigurationError(RuntimeError):
    """Configuration error whose text never includes a secret or token."""


class TelegramWebhookConflictError(RuntimeError):
    """Refuse polling while a webhook remains active unless explicitly cleared."""


@dataclass(frozen=True)
class TelegramRunnerConfiguration:
    bot_token: str
    identity_secret: str
    clear_webhook_on_start: bool
    drop_pending_updates: bool
    poll_timeout_seconds: int


def validate_runner_configuration(config=settings) -> TelegramRunnerConfiguration:
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

    return TelegramRunnerConfiguration(
        bot_token=token.strip(),
        identity_secret=secret,
        clear_webhook_on_start=strict_boolean("TELEGRAM_CLEAR_WEBHOOK_ON_START", False),
        drop_pending_updates=strict_boolean("TELEGRAM_DROP_PENDING_UPDATES", False),
        poll_timeout_seconds=parsed_timeout,
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


def build_application(config=settings, **handler_dependencies):
    configure_safe_logging()
    runner_config = validate_runner_configuration(config)
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    except ImportError as error:
        raise TelegramRunnerConfigurationError(
            "Telegram dependency is unavailable. Install the project dependencies."
        ) from error

    handlers = TelegramCustomerHandlers(
        identity_secret=runner_config.identity_secret,
        **handler_dependencies,
    )
    application = (
        ApplicationBuilder()
        .token(runner_config.bot_token)
        .concurrent_updates(False)
        .post_init(prepare_polling)
        .build()
    )
    application.bot_data["aura_runner_config"] = runner_config
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("status", handlers.status))
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
        runner_config = validate_runner_configuration(settings)
        application = build_application(settings)
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
