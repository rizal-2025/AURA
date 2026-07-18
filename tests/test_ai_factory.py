import unittest
from unittest.mock import patch

from app.services.ai.factory import get_ai_provider
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


class TestAIProviderFactory(unittest.TestCase):
    def test_returns_ollama_provider_when_configured(self):
        with patch("app.services.ai.factory.settings.AI_PROVIDER", "ollama"):
            provider = get_ai_provider()
            self.assertIsInstance(provider, OllamaProvider)

    def test_returns_openai_provider_when_configured(self):
        with patch("app.services.ai.factory.settings.AI_PROVIDER", "openai"):
            provider = get_ai_provider()
            self.assertIsInstance(provider, OpenAIProvider)


if __name__ == "__main__":
    unittest.main()
