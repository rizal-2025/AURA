from openai import AsyncOpenAI

from app.core.config import get_ai_settings
from app.services.ai.base import AIProvider


class OpenAIProvider(AIProvider):

    def __init__(self, config=None):
        self.config = config or get_ai_settings()
        self.client = AsyncOpenAI(
            api_key=self.config.OPENAI_API_KEY
        )

    async def chat(self, message: str) -> str:

        response = await self.client.responses.create(
            model=self.config.OPENAI_MODEL,
            input=message
        )

        return response.output_text
