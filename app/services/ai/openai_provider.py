from openai import AsyncOpenAI

from app.core.config import settings
from app.services.ai.base import AIProvider


class OpenAIProvider(AIProvider):

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY or "dummy-key"
        )

    async def chat(self, message: str) -> str:

        response = await self.client.responses.create(
            model=settings.OPENAI_MODEL,
            input=message
        )

        return response.output_text