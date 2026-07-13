from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "AURA"

    VERSION: str = "1.0.0"

    DATABASE_URL: str

    OPENAI_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=".env"
    )

settings = Settings()