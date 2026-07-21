import logging
import os
import re


_BOT_URL_PATTERN = re.compile(r"/bot[^/\s]+/", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_DATABASE_PASSWORD_PATTERN = re.compile(
    r"(?P<prefix>[a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@",
    re.IGNORECASE,
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
        "AUTH_JWT_SECRET",
        "OPENAI_API_KEY",
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


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
)

logger = logging.getLogger("AURA")


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
        if not any(isinstance(item, SensitiveDataFilter) for item in target_logger.filters):
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


configure_safe_logging()
