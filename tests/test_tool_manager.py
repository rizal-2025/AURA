import asyncio
import unittest

from app.tools.database_tool import DatabaseTool
from app.tools.manager import ToolManager


class TestToolManager(unittest.TestCase):
    def test_database_tool_executes_query(self):
        tool = DatabaseTool()
        result = asyncio.run(tool.execute(query="SELECT 1"))

        self.assertEqual(result["status"], "ok")
        self.assertIn("SELECT 1", result["message"])

    def test_tool_manager_executes_registered_tool(self):
        manager = ToolManager()
        result = asyncio.run(manager.execute("database", query="SELECT 2"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query"], "SELECT 2")

    def test_tool_manager_returns_error_for_unknown_tool(self):
        manager = ToolManager()
        result = asyncio.run(manager.execute("unknown", query="SELECT 3"))

        self.assertEqual(result["status"], "error")
        self.assertIn("not registered", result["message"])
