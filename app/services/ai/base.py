from abc import ABC, abstractmethod

class AIProvider(ABC):

    @abstractmethod
    async def chat(
        self,
        message: str,
        *,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        pass
