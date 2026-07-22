import re

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


MINIMUM_JWT_SECRET_LENGTH = 32
MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH = 32
MAXIMUM_JWT_EXPIRE_MINUTES = 1440
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 30
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS = 5
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS = 5
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS = 10
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS = 60


class Settings(BaseSettings):
    APP_NAME: str = "AURA"
    VERSION: str = "1.0.0"

    AI_PROVIDER: str = "ollama"
    OLLAMA_BASE_URL: str = "http://localhost:11434/v1"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    OPENAI_MODEL: str = "gpt-5"
    DATABASE_URL: str
    OPENAI_API_KEY: str | None = None

    AUTH_JWT_SECRET: str | None = None
    AUTH_JWT_ISSUER: str = "aura"
    AUTH_JWT_AUDIENCE: str = "aura-api"
    AUTH_JWT_EXPIRE_MINUTES: int = 60
    SQL_ECHO: bool = False

    # Telegram is optional for the FastAPI process. The runner validates these
    # values immediately before starting local long polling.
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_IDENTITY_SECRET: str | None = None
    # Keep Telegram values unparsed here. FastAPI does not depend on the runner
    # and must remain startable even when optional Telegram settings are bad.
    TELEGRAM_CLEAR_WEBHOOK_ON_START: object = False
    TELEGRAM_DROP_PENDING_UPDATES: object = False
    TELEGRAM_POLL_TIMEOUT_SECONDS: object = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    TELEGRAM_OWNER_NOTIFICATIONS_ENABLED: object = False
    TELEGRAM_OWNER_CHAT_ID: object = None
    TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS
    TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS
    TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS
    TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS: object = DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        validate_default=True,
    )

    @field_validator("AUTH_JWT_SECRET", mode="before")
    @classmethod
    def validate_jwt_secret(cls, value: str | None) -> str:
        if not isinstance(value, str) or len(value) < MINIMUM_JWT_SECRET_LENGTH:
            raise ValueError(
                "AUTH_JWT_SECRET must be configured and be at least "
                f"{MINIMUM_JWT_SECRET_LENGTH} characters long."
            )
        return value

    @field_validator("AUTH_JWT_EXPIRE_MINUTES", mode="before")
    @classmethod
    def validate_jwt_expiry(cls, value: int | str) -> int:
        if isinstance(value, bool):
            raise ValueError("AUTH_JWT_EXPIRE_MINUTES must be a strict integer.")
        if isinstance(value, int):
            expires_minutes = value
        elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            expires_minutes = int(value)
        else:
            raise ValueError("AUTH_JWT_EXPIRE_MINUTES must be a strict integer.")

        if not 1 <= expires_minutes <= MAXIMUM_JWT_EXPIRE_MINUTES:
            raise ValueError(
                "AUTH_JWT_EXPIRE_MINUTES must be between 1 and "
                f"{MAXIMUM_JWT_EXPIRE_MINUTES}."
            )
        return expires_minutes

try:
    settings = Settings()
except ValidationError as error:
    invalid_fields = {item["loc"][0] for item in error.errors()}
    if "AUTH_JWT_SECRET" in invalid_fields:
        raise RuntimeError(
            "Invalid AURA authentication configuration: AUTH_JWT_SECRET must be "
            f"configured and at least {MINIMUM_JWT_SECRET_LENGTH} characters long."
        ) from None
    if "AUTH_JWT_EXPIRE_MINUTES" in invalid_fields:
        raise RuntimeError(
            "Invalid AURA authentication configuration: "
            "AUTH_JWT_EXPIRE_MINUTES must be a strict integer between 1 and "
            f"{MAXIMUM_JWT_EXPIRE_MINUTES}."
        ) from None
    raise RuntimeError(
        "Invalid AURA configuration. Check required environment variables."
    ) from None
