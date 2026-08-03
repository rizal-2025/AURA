import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.orchestrator import AgentOrchestrator
from app.api.chat import chat
from app.api.dependencies import get_current_customer
from app.db.database import get_db
from app.agents.result import AgentTurnResult
from app.core.conversation_memory import build_authenticated_memory_key
from app.main import create_app
from app.schemas.chat import ChatRequest
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.reservation.dto import PersistedReservationDTO


SEEDED_HTTP_RESERVATION_ID = (2**30) + 104_801
HTTP_REFERENCE = "RSV_" + "d1" * 16


class DummyDB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class TestChatIntegration(unittest.TestCase):
    def test_chat_uses_workflow_for_reservation_intent(self):
        request = ChatRequest(session_id="abc", message="Saya mau reservasi")
        customer = SimpleNamespace(id=uuid4())

        async def fake_handle(*args, **kwargs):
            return AgentTurnResult(reply="Atas nama siapa reservasinya?")

        with patch(
            "app.api.chat.agent.handle_turn",
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

    def test_http_reply_from_shared_agent_path_omits_exact_seeded_id(self):
        customer = SimpleNamespace(id=uuid4(), is_active=True, token_version=0)
        session_reference = "seeded-http-session"
        memory_key = build_authenticated_memory_key(
            customer.id,
            session_reference,
        )
        orchestrator = AgentOrchestrator()
        orchestrator.memory_manager.update_session(
            memory_key,
            {
                "intent": "reservation",
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
                "completed": False,
                "awaiting_confirmation": True,
                "editing_field": None,
                "asked_fields": ["name", "people", "date", "time"],
            },
        )
        persisted = PersistedReservationDTO(
            id=SEEDED_HTTP_RESERVATION_ID,
            name="Rizal",
            people=4,
            date="2026-08-01",
            time="19:00",
            status="pending",
            reference=HTTP_REFERENCE,
        )
        reservation_agent = orchestrator.workflow._agents["reservation"]
        service = AuthenticatedChatService(agent=orchestrator)
        application = create_app(
            SimpleNamespace(APP_ENV="test", APP_NAME="AURA", VERSION="test")
        )
        application.dependency_overrides[get_db] = lambda: DummyDB()
        application.dependency_overrides[get_current_customer] = lambda: customer

        with patch.object(
            orchestrator.handoff_service,
            "restore_active_handoff",
            return_value=None,
        ), patch.object(
            reservation_agent.reservation_service,
            "create_reservation",
            return_value=persisted,
        ) as create_reservation, patch(
            "app.api.chat.authenticated_chat_service",
            service,
        ), TestClient(application) as client:
            response = client.post(
                "/chat",
                json={"session_id": session_reference, "message": "Ya"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"reply"})
        self.assertIn(HTTP_REFERENCE, payload["reply"])
        self.assertNotIn(str(SEEDED_HTTP_RESERVATION_ID), payload["reply"])
        self.assertNotIn("reservation_operation", payload)
        self.assertNotIn("reservationOperation", payload)
        create_reservation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
