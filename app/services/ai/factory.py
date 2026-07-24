from app.core.config import get_ai_settings
from app.core.config_validation import (
    CFG_AI_OLLAMA_INVALID,
    CFG_AI_OPENAI_INVALID,
    CFG_AI_PROVIDER_INVALID,
    ConfigurationError,
    validate_ai_provider,
    validate_app_environment,
    validate_model_name,
    validate_ollama_url,
    validate_openai_api_key,
)
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


def get_ai_provider(config=None):
    config = config or get_ai_settings()
    provider_name = validate_ai_provider(getattr(config, "AI_PROVIDER", None))
    app_env = validate_app_environment(getattr(config, "APP_ENV", None))

    if provider_name == "openai":
        validate_openai_api_key(getattr(config, "OPENAI_API_KEY", None))
        validate_model_name(
            getattr(config, "OPENAI_MODEL", None),
            code=CFG_AI_OPENAI_INVALID,
        )
        return OpenAIProvider(config)
    if provider_name == "ollama":
        validate_ollama_url(
            getattr(config, "OLLAMA_BASE_URL", None),
            app_env=app_env,
        )
        validate_model_name(
            getattr(config, "OLLAMA_MODEL", None),
            code=CFG_AI_OLLAMA_INVALID,
        )
        return OllamaProvider(config)

    raise ConfigurationError(CFG_AI_PROVIDER_INVALID)
