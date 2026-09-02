from openai import AsyncOpenAI

from app.core.config import get_ai_settings
from app.services.ai.base import AIProvider


class OllamaProvider(AIProvider):

    def __init__(self, config=None):
        self.config = config or get_ai_settings()
        self.client = AsyncOpenAI(
            base_url=self.config.OLLAMA_BASE_URL,
            api_key="ollama",
            timeout=getattr(self.config, "AI_PROVIDER_TIMEOUT_SECONDS", 20),
            max_retries=0,
        )

    async def chat(
        self,
        message: str,
        *,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        if instructions is not None:
            message = f"{instructions}\n\n{message}"
        request = {
            "model": self.config.OLLAMA_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
        }
        if max_output_tokens is not None:
            request["max_tokens"] = max_output_tokens

        response = await self.client.chat.completions.create(**request)

        return response.choices[0].message.content or ""
