import re

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


MINIMUM_JWT_SECRET_LENGTH = 32
MAXIMUM_JWT_EXPIRE_MINUTES = 1440


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
