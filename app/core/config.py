from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )


settings = Settings()
