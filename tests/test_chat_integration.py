import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.api.chat import chat
from app.core.conversation_memory import build_authenticated_memory_key
from app.schemas.chat import ChatRequest


class DummyDB:
    def close(self):
        pass


class TestChatIntegration(unittest.TestCase):
    def test_chat_uses_workflow_for_reservation_intent(self):
        request = ChatRequest(session_id="abc", message="Saya mau reservasi")
        customer = SimpleNamespace(id=uuid4())

        async def fake_handle(*args, **kwargs):
            return "Atas nama siapa reservasinya?"

        with patch(
            "app.api.chat.agent.handle",
            new=AsyncMock(side_effect=fake_handle),
        ) as agent_handle:
            response = asyncio.run(chat(request, DummyDB(), customer))
            self.assertTrue(hasattr(response, "reply"))
            self.assertIn("reservasinya", response.reply)
            agent_handle.assert_awaited_once_with(
                session_id=build_authenticated_memory_key(customer.id, "abc"),
                message="Saya mau reservasi",
                db=unittest.mock.ANY,
                owner_customer_id=customer.id,
            )


if __name__ == "__main__":
    unittest.main()
