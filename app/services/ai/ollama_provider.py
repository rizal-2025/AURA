from openai import AsyncOpenAI

from app.services.ai.base import AIProvider


class OllamaProvider(AIProvider):

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama"  # Ollama tidak memerlukan API key asli
        )

    async def chat(self, message: str) -> str:

        response = await self.client.responses.create(
            model="qwen2.5:3b",
            input=message,
        )

        return response.output_text