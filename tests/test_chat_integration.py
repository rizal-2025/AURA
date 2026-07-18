import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.api.chat import chat
from app.schemas.chat import ChatRequest


class DummyDB:
    def close(self):
        pass


class TestChatIntegration(unittest.TestCase):
    def test_chat_uses_workflow_for_reservation_intent(self):
        request = ChatRequest(session_id="abc", message="Saya mau reservasi")

        async def fake_handle(*args, **kwargs):
            return "Atas nama siapa reservasinya?"

        with patch("app.api.chat.agent.handle", new=AsyncMock(side_effect=fake_handle)):
            response = asyncio.run(chat(request, DummyDB()))
            self.assertTrue(hasattr(response, "reply"))
            self.assertIn("reservasinya", response.reply)


if __name__ == "__main__":
    unittest.main()
