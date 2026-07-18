import asyncio
import unittest

from app.agents.orchestrator import AgentOrchestrator
from app.agents.reservation_agent import ReservationAgent
from app.brain.memory_manager import MemoryManager


class TestReservationConfirmationFlow(unittest.TestCase):
    def test_confirmation_success_flow(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": False}
        memory.update_session("s1", state)

        result = asyncio.run(
            agent.handle_confirmation("ya", "s1")
        )

        self.assertIn("Reservasi berhasil dibuat", result["response"])
        self.assertEqual(memory.get_session("s1")["completed"], True)

    def test_rejection_flow(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": True}
        memory.update_session("s1", state)

        result = asyncio.run(agent.handle_confirmation("tidak", "s1"))

        self.assertIn("Silakan kirim data yang ingin diperbaiki", result["response"])
        self.assertEqual(memory.get_session("s1")["awaiting_confirmation"], False)

    def test_update_after_rejection(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": True}
        memory.update_session("s1", state)

        asyncio.run(agent.handle_confirmation("tidak", "s1"))
        memory.update_session("s1", {"time": "20:00"})
        state = memory.get_session("s1")

        self.assertEqual(state["time"], "20:00")

    def test_memory_state_updates(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": False}
        memory.update_session("s1", state)

        asyncio.run(agent.handle_confirmation("ya", "s1"))
        state = memory.get_session("s1")

        self.assertEqual(state["completed"], True)
        self.assertEqual(state["awaiting_confirmation"], False)

    def test_orchestrator_accepts_confirmation_and_completes_reservation(self):
        orchestrator = AgentOrchestrator()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()
        orchestrator.memory_manager.update_session("s-confirm", {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
        })

        result = asyncio.run(orchestrator.handle("s-confirm", "ya", None))
        session = orchestrator.memory_manager.get_session("s-confirm")

        self.assertIn("Reservasi berhasil dibuat", result)
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        self.assertIn("reservation_id", session)

    def test_orchestrator_rejects_confirmation_and_exits_confirmation_mode(self):
        orchestrator = AgentOrchestrator()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()
        orchestrator.memory_manager.update_session("s-reject", {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
        })

        result = asyncio.run(orchestrator.handle("s-reject", "tidak", None))
        session = orchestrator.memory_manager.get_session("s-reject")

        self.assertIn("field", result.lower())
        self.assertFalse(session["awaiting_confirmation"])


if __name__ == "__main__":
    unittest.main()
