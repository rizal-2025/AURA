from openai import AsyncOpenAI

from app.core.config import get_ai_settings
from app.services.ai.base import AIProvider


class OllamaProvider(AIProvider):

    def __init__(self, config=None):
        self.config = config or get_ai_settings()
        self.client = AsyncOpenAI(
            base_url=self.config.OLLAMA_BASE_URL,
            api_key="ollama"
        )

    async def chat(self, message: str) -> str:

        response = await self.client.responses.create(
            model=self.config.OLLAMA_MODEL,
            input=message,
        )

        return response.output_text
