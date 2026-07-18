from typing import Any

from app.tools.base import BaseTool
from app.tools.database_tool import DatabaseTool


class ToolManager:
    """Registry for tools that agents can call using the same interface."""

    def __init__(self, tools: dict[str, BaseTool] | None = None):
        self._tools = tools or {
            "database": DatabaseTool(),
        }

    async def execute(self, tool_name: str, **kwargs: Any) -> Any:
        tool = self._tools.get(tool_name)
        if tool is None:
            return {"status": "error", "message": f"Tool '{tool_name}' is not registered"}
        return await tool.execute(**kwargs)

    def register(self, name: str, tool: BaseTool) -> None:
        self._tools[name] = tool
