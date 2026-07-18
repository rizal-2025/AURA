from typing import Any

from app.tools.base import BaseTool


class DatabaseTool(BaseTool):
    """Example tool for querying existing application data through a shared interface."""

    name = "database"

    async def execute(self, **kwargs: Any) -> Any:
        query = kwargs.get("query")
        if not query:
            return {"status": "error", "message": "Query is required"}

        return {
            "status": "ok",
            "query": query,
            "message": f"DatabaseTool executed: {query}",
        }
