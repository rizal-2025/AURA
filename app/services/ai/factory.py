from app.core.config import settings
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


def get_ai_provider():
    provider_name = getattr(settings, "AI_PROVIDER", "ollama").lower()

    if provider_name == "openai":
        return OpenAIProvider()

    return OllamaProvider()