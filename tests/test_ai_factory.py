import unittest
from types import SimpleNamespace

from app.core.config_validation import ConfigurationError
from app.services.ai.factory import get_ai_provider
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


class TestAIProviderFactory(unittest.TestCase):
    def test_returns_ollama_provider_when_configured(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="qwen2.5:3b",
        ))
        self.assertIsInstance(provider, OllamaProvider)

    def test_returns_openai_provider_when_configured(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="openai",
            OPENAI_API_KEY="test-openai-key-not-for-production-12345",
            OPENAI_MODEL="gpt-test",
        ))
        self.assertIsInstance(provider, OpenAIProvider)

    def test_unknown_provider_does_not_fall_back_to_ollama(self):
        with self.assertRaises(ConfigurationError):
            get_ai_provider(SimpleNamespace(APP_ENV="test", AI_PROVIDER="unknown"))


if __name__ == "__main__":
    unittest.main()
