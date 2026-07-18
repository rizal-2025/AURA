from openai import AsyncOpenAI

from app.core.config import settings
from app.services.ai.base import AIProvider


class OllamaProvider(AIProvider):

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.OLLAMA_BASE_URL,
            api_key="ollama"
        )

    async def chat(self, message: str) -> str:

        response = await self.client.responses.create(
            model=settings.OLLAMA_MODEL,
            input=message,
        )

        return response.output_text