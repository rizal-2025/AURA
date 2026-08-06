"""Focused, lazy configuration boundaries for AURA processes."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config_validation import (
    CFG_AI_OLLAMA_INVALID,
    CFG_AI_OPENAI_INVALID,
    CFG_AI_PROVIDER_INVALID,
    CFG_AI_TIMEOUT_INVALID,
    CFG_AUTH_AUDIENCE_INVALID,
    CFG_AUTH_EXPIRY_INVALID,
    CFG_AUTH_ISSUER_INVALID,
    CFG_AUTH_SECRET_INVALID,
    CFG_DATABASE_INVALID,
    CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
    CFG_DEMO_DATABASE_NAME_INVALID,
    CFG_DEMO_DATABASE_REQUIRED,
    CFG_DEMO_DATABASE_SAME_TARGET,
    CFG_ENV_INVALID,
    MAXIMUM_JWT_EXPIRE_MINUTES,
    MINIMUM_SECRET_LENGTH,
    ConfigurationError,
    parse_strict_boolean,
    parse_strict_positive_integer,
    select_database_url,
    validate_ai_provider,
    validate_app_environment,
    validate_deployed_identity_label,
    validate_model_name,
    validate_ollama_url,
    validate_openai_api_key,
    validate_secret,
)


MINIMUM_JWT_SECRET_LENGTH = MINIMUM_SECRET_LENGTH
MINIMUM_TELEGRAM_IDENTITY_SECRET_LENGTH = MINIMUM_SECRET_LENGTH
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 30
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS = 5
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS = 5
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS = 10
DEFAULT_TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS = 60

_BASE_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    extra="ignore",
    validate_default=True,
    hide_input_in_errors=True,
    frozen=True,
)


def _safe_code_from_validation(error: ValidationError, fallback: str) -> str:
    for item in error.errors(include_input=False):
        message = str(item.get("msg", ""))
        for code in (
            CFG_ENV_INVALID,
            CFG_DATABASE_INVALID,
            CFG_DEMO_DATABASE_REQUIRED,
            CFG_DEMO_DATABASE_SAME_TARGET,
            CFG_DEMO_DATABASE_NAME_INVALID,
            CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
            CFG_AUTH_SECRET_INVALID,
            CFG_AUTH_EXPIRY_INVALID,
            CFG_AUTH_ISSUER_INVALID,
            CFG_AUTH_AUDIENCE_INVALID,
            CFG_AI_PROVIDER_INVALID,
            CFG_AI_OPENAI_INVALID,
            CFG_AI_OLLAMA_INVALID,
            CFG_AI_TIMEOUT_INVALID,
        ):
            if code in message:
                return code
    return fallback


class AppEnvironmentSettings(BaseSettings):
    APP_ENV: object
    APP_NAME: str = "AURA"
    VERSION: str = "1.0.0"

    model_config = _BASE_SETTINGS_CONFIG

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def validate_environment(cls, value):
        return validate_app_environment(value)


class DatabaseSettings(BaseSettings):
    APP_ENV: object = None
    DATABASE_URL: object = Field(default=None, repr=False)
    DEMO_DATABASE_URL: object = Field(default=None, repr=False)
    SQL_ECHO: object = False

    model_config = _BASE_SETTINGS_CONFIG

    @model_validator(mode="after")
    def select_active_database(self):
        if self.APP_ENV is None:
            # Preserve the focused builder's legacy use in isolated unit tests.
            selected = select_database_url(
                app_env="development",
                database_url=self.DATABASE_URL,
                demo_database_url=None,
            )
        else:
            selected = select_database_url(
                app_env=self.APP_ENV,
                database_url=self.DATABASE_URL,
                demo_database_url=self.DEMO_DATABASE_URL,
            )
        object.__setattr__(self, "DATABASE_URL", selected)
        if self.APP_ENV == "demo":
            object.__setattr__(self, "SQL_ECHO", False)
        return self

    @field_validator("SQL_ECHO", mode="before")
    @classmethod
    def validate_sql_echo(cls, value):
        return parse_strict_boolean(value, code=CFG_DATABASE_INVALID)


class DemoSettings(BaseSettings):
    APP_ENV: object
    DEMO_BFF_SERVICE_TOKEN: SecretStr | None = Field(
        default=None,
        repr=False,
    )

    model_config = _BASE_SETTINGS_CONFIG

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def validate_environment(cls, value):
        return validate_app_environment(value)

    @model_validator(mode="after")
    def validate_bff_service_token(self):
        if self.APP_ENV != "demo":
            object.__setattr__(self, "DEMO_BFF_SERVICE_TOKEN", None)
            return self
        secret = self.DEMO_BFF_SERVICE_TOKEN
        raw_secret = (
            secret.get_secret_value()
            if isinstance(secret, SecretStr)
            else secret
        )
        validated = validate_secret(
            raw_secret,
            code=CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        )
        object.__setattr__(
            self,
            "DEMO_BFF_SERVICE_TOKEN",
            SecretStr(validated),
        )
        return self


class AuthSettings(BaseSettings):
    APP_ENV: object
    AUTH_JWT_SECRET: object = Field(repr=False)
    AUTH_JWT_ISSUER: object = "aura"
    AUTH_JWT_AUDIENCE: object = "aura-api"
    AUTH_JWT_EXPIRE_MINUTES: object = 60

    model_config = _BASE_SETTINGS_CONFIG

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def validate_environment(cls, value):
        return validate_app_environment(value)

    @field_validator("AUTH_JWT_SECRET", mode="before")
    @classmethod
    def validate_jwt_secret(cls, value):
        return validate_secret(value, code=CFG_AUTH_SECRET_INVALID)

    @field_validator("AUTH_JWT_EXPIRE_MINUTES", mode="before")
    @classmethod
    def validate_jwt_expiry(cls, value):
        return parse_strict_positive_integer(
            value,
            minimum=1,
            maximum=MAXIMUM_JWT_EXPIRE_MINUTES,
            code=CFG_AUTH_EXPIRY_INVALID,
        )

    @model_validator(mode="after")
    def validate_identity_labels(self):
        object.__setattr__(
            self,
            "AUTH_JWT_ISSUER",
            validate_deployed_identity_label(
                self.AUTH_JWT_ISSUER,
                app_env=self.APP_ENV,
                development_value="aura",
                code=CFG_AUTH_ISSUER_INVALID,
            ),
        )
        object.__setattr__(
            self,
            "AUTH_JWT_AUDIENCE",
            validate_deployed_identity_label(
                self.AUTH_JWT_AUDIENCE,
                app_env=self.APP_ENV,
                development_value="aura-api",
                code=CFG_AUTH_AUDIENCE_INVALID,
            ),
        )
        return self


class AISettings(BaseSettings):
    APP_ENV: object
    AI_PROVIDER: object
    OLLAMA_BASE_URL: object = None
    OLLAMA_MODEL: object = None
    OPENAI_MODEL: object = None
    OPENAI_API_KEY: object = Field(default=None, repr=False)
    AI_PROVIDER_TIMEOUT_SECONDS: object = 20

    model_config = _BASE_SETTINGS_CONFIG

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def validate_environment(cls, value):
        return validate_app_environment(value)

    @field_validator("AI_PROVIDER", mode="before")
    @classmethod
    def validate_provider(cls, value):
        return validate_ai_provider(value)

    @field_validator("AI_PROVIDER_TIMEOUT_SECONDS", mode="before")
    @classmethod
    def validate_provider_timeout(cls, value):
        return parse_strict_positive_integer(
            value,
            minimum=1,
            maximum=30,
            code=CFG_AI_TIMEOUT_INVALID,
        )

    @model_validator(mode="after")
    def validate_selected_provider(self):
        if self.AI_PROVIDER == "openai":
            object.__setattr__(
                self,
                "OPENAI_API_KEY",
                validate_openai_api_key(self.OPENAI_API_KEY),
            )
            object.__setattr__(
                self,
                "OPENAI_MODEL",
                validate_model_name(
                    self.OPENAI_MODEL,
                    code=CFG_AI_OPENAI_INVALID,
                ),
            )
        else:
            object.__setattr__(
                self,
                "OLLAMA_BASE_URL",
                validate_ollama_url(
                    self.OLLAMA_BASE_URL,
                    app_env=self.APP_ENV,
                ),
            )
            object.__setattr__(
                self,
                "OLLAMA_MODEL",
                validate_model_name(
                    self.OLLAMA_MODEL,
                    code=CFG_AI_OLLAMA_INVALID,
                ),
            )
        return self


@dataclass(frozen=True)
class ApplicationSettings:
    environment: AppEnvironmentSettings
    database: DatabaseSettings
    demo: DemoSettings
    auth: AuthSettings
    ai: AISettings

    _FIELD_COMPONENT = {
        "APP_ENV": "environment",
        "APP_NAME": "environment",
        "VERSION": "environment",
        "DATABASE_URL": "database",
        "DEMO_DATABASE_URL": "database",
        "SQL_ECHO": "database",
        "DEMO_BFF_SERVICE_TOKEN": "demo",
        "AUTH_JWT_SECRET": "auth",
        "AUTH_JWT_ISSUER": "auth",
        "AUTH_JWT_AUDIENCE": "auth",
        "AUTH_JWT_EXPIRE_MINUTES": "auth",
        "AI_PROVIDER": "ai",
        "OLLAMA_BASE_URL": "ai",
        "OLLAMA_MODEL": "ai",
        "OPENAI_MODEL": "ai",
        "OPENAI_API_KEY": "ai",
        "AI_PROVIDER_TIMEOUT_SECONDS": "ai",
    }

    def __getattr__(self, name):
        component_name = self._FIELD_COMPONENT.get(name)
        if component_name is None:
            raise AttributeError(name)
        return getattr(getattr(self, component_name), name)

def _construct(model, fallback_code: str, **values):
    if (
        "_env_file" not in values
        and os.environ.get("AURA_DISABLE_DOTENV") == "1"
    ):
        values["_env_file"] = None
    try:
        return model(**values)
    except ConfigurationError:
        raise
    except ValidationError as error:
        raise ConfigurationError(
            _safe_code_from_validation(error, fallback_code)
        ) from None


def _settings_values(values):
    return {key: value for key, value in values.items() if not key.startswith("_")}


def build_environment_settings(*, _env_file=".env", **values) -> AppEnvironmentSettings:
    return _construct(
        AppEnvironmentSettings,
        CFG_ENV_INVALID,
        _env_file=_env_file,
        **_settings_values(values),
    )


def build_database_settings(*, _env_file=".env", **values) -> DatabaseSettings:
    return _construct(
        DatabaseSettings,
        CFG_DATABASE_INVALID,
        _env_file=_env_file,
        **_settings_values(values),
    )


def build_auth_settings(*, _env_file=".env", **values) -> AuthSettings:
    return _construct(
        AuthSettings,
        CFG_AUTH_SECRET_INVALID,
        _env_file=_env_file,
        **_settings_values(values),
    )


def build_demo_settings(*, _env_file=".env", **values) -> DemoSettings:
    return _construct(
        DemoSettings,
        CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        _env_file=_env_file,
        **_settings_values(values),
    )


def build_ai_settings(*, _env_file=".env", **values) -> AISettings:
    return _construct(
        AISettings,
        CFG_AI_PROVIDER_INVALID,
        _env_file=_env_file,
        **_settings_values(values),
    )


def build_application_settings(*, _env_file=".env", **values) -> ApplicationSettings:
    values = _settings_values(values)
    environment = build_environment_settings(
        _env_file=_env_file,
        **values,
    )
    scoped_values = dict(values)
    scoped_values["APP_ENV"] = environment.APP_ENV
    return ApplicationSettings(
        environment=environment,
        database=build_database_settings(
            _env_file=_env_file,
            **scoped_values,
        ),
        demo=build_demo_settings(
            _env_file=_env_file,
            **scoped_values,
        ),
        auth=build_auth_settings(
            _env_file=_env_file,
            **scoped_values,
        ),
        ai=build_ai_settings(
            _env_file=_env_file,
            **scoped_values,
        ),
    )


class Settings:
    """Compatibility constructor returning the focused application aggregate."""

    def __new__(cls, _env_file=".env", **values):
        return build_application_settings(_env_file=_env_file, **values)


@lru_cache
def get_environment_settings() -> AppEnvironmentSettings:
    return _construct(AppEnvironmentSettings, CFG_ENV_INVALID)


@lru_cache
def get_database_settings() -> DatabaseSettings:
    environment = get_environment_settings()
    return _construct(
        DatabaseSettings,
        CFG_DATABASE_INVALID,
        APP_ENV=environment.APP_ENV,
    )


@lru_cache
def get_auth_settings() -> AuthSettings:
    environment = get_environment_settings()
    return _construct(
        AuthSettings,
        CFG_AUTH_SECRET_INVALID,
        APP_ENV=environment.APP_ENV,
    )


@lru_cache
def get_demo_settings() -> DemoSettings:
    environment = get_environment_settings()
    return _construct(
        DemoSettings,
        CFG_DEMO_BFF_SERVICE_TOKEN_INVALID,
        APP_ENV=environment.APP_ENV,
    )


@lru_cache
def get_ai_settings() -> AISettings:
    environment = get_environment_settings()
    return _construct(
        AISettings,
        CFG_AI_PROVIDER_INVALID,
        APP_ENV=environment.APP_ENV,
    )


@lru_cache
def get_application_settings() -> ApplicationSettings:
    return ApplicationSettings(
        environment=get_environment_settings(),
        database=get_database_settings(),
        demo=get_demo_settings(),
        auth=get_auth_settings(),
        ai=get_ai_settings(),
    )


def clear_settings_cache() -> None:
    get_application_settings.cache_clear()
    get_ai_settings.cache_clear()
    get_demo_settings.cache_clear()
    get_auth_settings.cache_clear()
    get_database_settings.cache_clear()
    get_environment_settings.cache_clear()


class _LazyApplicationSettings:
    def __getattr__(self, name):
        return getattr(get_application_settings(), name)

    def __setattr__(self, name, value):
        raise TypeError(
            "AURA settings are immutable; use validated factories or scoped "
            "dependency injection."
        )


settings = _LazyApplicationSettings()
