import asyncio
import unittest

from app.agents.orchestrator import AgentOrchestrator
from app.agents.workflow import AgentWorkflow
from app.brain.planner import Planner


class TestReservationStatePersistence(unittest.TestCase):
    def test_reservation_state_persists_across_requests_for_same_session(self):
        orchestrator = AgentOrchestrator()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()

        first_response = asyncio.run(orchestrator.handle("session-123", "Saya mau reservasi", None))
        second_response = asyncio.run(orchestrator.handle("session-123", "Rizal", None))

        session_state = orchestrator.memory_manager.get_session("session-123")

        self.assertIn("Atas nama", first_response)
        self.assertEqual(second_response, "Untuk berapa orang?")
        self.assertEqual(session_state.get("name"), "Rizal")

    def test_reservation_state_is_isolated_per_session(self):
        orchestrator = AgentOrchestrator()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()

        asyncio.run(orchestrator.handle("session-a", "Saya mau reservasi", None))
        asyncio.run(orchestrator.handle("session-a", "Rizal", None))
        asyncio.run(orchestrator.handle("session-b", "Saya mau reservasi", None))

        session_a = orchestrator.memory_manager.get_session("session-a")
        session_b = orchestrator.memory_manager.get_session("session-b")

        self.assertEqual(session_a.get("name"), "Rizal")
        self.assertIsNone(session_b.get("name"))
