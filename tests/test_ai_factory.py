import unittest
from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

from app.core.config_validation import ConfigurationError
from app.services.ai.factory import get_ai_provider
from app.services.ai.ollama_provider import OllamaProvider
from app.services.ai.openai_provider import OpenAIProvider


class TestAIProviderFactory(unittest.TestCase):
    def test_ollama_chat_uses_chat_completions_api(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="qwen2.5:3b",
        ))

        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="AURA OLLAMA OK")
                )
            ]
        )

        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=response)
                )
            )
        )

        result = asyncio.run(provider.chat("Tes Ollama"))

        self.assertEqual(result, "AURA OLLAMA OK")
        provider.client.chat.completions.create.assert_awaited_once_with(
            model="qwen2.5:3b",
            messages=[
                {
                    "role": "user",
                    "content": "Tes Ollama",
                }
            ],
        )

    def test_ollama_forwards_bounded_general_output_tokens(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="qwen2.5:3b",
        ))
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="bounded"))]
        )
        create = AsyncMock(return_value=response)
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )

        result = asyncio.run(
            provider.chat("General prompt", max_output_tokens=300)
        )

        self.assertEqual(result, "bounded")
        self.assertEqual(create.await_args.kwargs["max_tokens"], 300)

    def test_ollama_preserves_separated_general_prompt_semantics(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://localhost:11434/v1",
            OLLAMA_MODEL="qwen2.5:3b",
        ))
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        create = AsyncMock(return_value=response)
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )

        result = asyncio.run(
            provider.chat(
                "Untrusted conversation data",
                instructions="Stable AURA instructions",
            )
        )

        self.assertEqual(result, "ok")
        create.assert_awaited_once_with(
            model="qwen2.5:3b",
            messages=[
                {
                    "role": "user",
                    "content": "Stable AURA instructions\n\n"
                    "Untrusted conversation data",
                }
            ],
        )

    def test_openai_forwards_bounded_general_output_tokens(self):
        provider = get_ai_provider(SimpleNamespace(
            APP_ENV="test",
            AI_PROVIDER="openai",
            OPENAI_API_KEY="test-openai-key-not-for-production-12345",
            OPENAI_MODEL="gpt-test",
        ))
        create = AsyncMock(
            return_value=SimpleNamespace(output_text="bounded")
        )
        provider.client = SimpleNamespace(
            responses=SimpleNamespace(create=create)
        )

        result = asyncio.run(
            provider.chat(
                "General input",
                instructions="Stable AURA instructions",
                max_output_tokens=300,
            )
        )

        self.assertEqual(result, "bounded")
        create.assert_awaited_once_with(
            model="gpt-test",
            input="General input",
            instructions="Stable AURA instructions",
            max_output_tokens=300,
        )

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
