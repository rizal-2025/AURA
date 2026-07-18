from typing import Any, Protocol


class Tool(Protocol):
    name: str

    async def execute(self, **kwargs: Any) -> Any:
        ...


class BaseTool:
    """Base class for tools that agents can call through a shared interface."""

    name = "base"

    async def execute(self, **kwargs: Any) -> Any:
        raise NotImplementedError
