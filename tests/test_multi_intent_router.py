import asyncio
import io
import logging
import unittest
from unittest.mock import AsyncMock

from app.agents.orchestrator import AgentOrchestrator
from app.agents.workflow import AgentWorkflow
from app.brain.classifier import IntentClassifier
from app.brain.planner import Planner
from app.brain.memory_manager import MemoryManager
from app.core.logger import logger
from app.services.handoff.service import HandoffService


class TestMultiIntentRouter(unittest.TestCase):
    def test_workflow_routes_reservation_to_reservation_agent(self):
        workflow = AgentWorkflow()
        planner = Planner()

        plan = asyncio.run(planner.plan({"intent": "reservation", "confidence": 0.95}, {"name": None, "people": None, "date": None, "time": None}))
        result = asyncio.run(workflow.execute(plan, {"name": None, "people": None, "date": None, "time": None}, "Saya mau reservasi"))

        self.assertEqual(result["status"], "awaiting_input")
        self.assertIn("Atas nama", result["response"])

    def test_workflow_routes_check_reservation_to_stub_agent(self):
        workflow = AgentWorkflow()
        plan = {"intent": "check_reservation", "steps": [{"agent": "check_reservation", "action": "check_reservation"}]}

        result = asyncio.run(workflow.execute(plan, {}, "Cek reservasi saya"))

        self.assertEqual(result["status"], "stub")
        self.assertIn("cek reservasi", result["response"].lower())

    def test_workflow_routes_cancel_reservation_to_stub_agent(self):
        workflow = AgentWorkflow()
        plan = {"intent": "cancel_reservation", "steps": [{"agent": "cancel_reservation", "action": "cancel_reservation"}]}

        result = asyncio.run(workflow.execute(plan, {}, "Batalkan reservasi"))

        self.assertEqual(result["status"], "stub")
        self.assertIn("batal", result["response"].lower())

    def test_workflow_routes_greeting_to_stub_agent(self):
        workflow = AgentWorkflow()
        plan = {"intent": "greeting", "steps": [{"agent": "greeting", "action": "greet"}]}

        result = asyncio.run(workflow.execute(plan, {}, "Halo"))

        self.assertEqual(result["status"], "stub")
        self.assertIn("halo", result["response"].lower())

    def test_workflow_routes_general_question_to_stub_agent(self):
        workflow = AgentWorkflow()
        plan = {"intent": "general_question", "steps": [{"agent": "general_question", "action": "answer"}]}

        result = asyncio.run(workflow.execute(plan, {}, "Apa kabar?"))

        self.assertEqual(result["status"], "stub")
        self.assertIn("pertanyaan", result["response"].lower())

    def test_orchestrator_uses_workflow_for_non_reservation_intents(self):
        orchestrator = AgentOrchestrator()

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "greeting", "confidence": 0.95}

        class DummyWorkflow:
            async def execute(self, plan, session_state, user_message, **kwargs):
                return {"status": "stub", "response": "stubbed greeting"}

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.workflow = DummyWorkflow()

        response = asyncio.run(
            orchestrator.handle("session-1", "Halo", None, "test-owner"),
        )

        self.assertEqual(response, "stubbed greeting")

    def test_deterministic_greeting_bypasses_failing_provider(self):
        provider = type("FailingProvider", (), {
            "chat": AsyncMock(side_effect=ConnectionError("private provider failure")),
        })()
        orchestrator = AgentOrchestrator()
        orchestrator.intent_classifier = IntentClassifier(provider=provider)
        orchestrator.ai = provider

        response = asyncio.run(
            orchestrator.handle("greeting-session", "Halo!", object(), "test-owner"),
        )

        self.assertIn("Halo! Saya AURA", response)
        provider.chat.assert_not_awaited()
        self.assertFalse(orchestrator.handoff_service.is_required("greeting-session"))

    def test_non_greeting_provider_failure_creates_safe_internal_handoff_without_raw_log(self):
        provider = type("FailingProvider", (), {
            "chat": AsyncMock(side_effect=ConnectionError("private provider failure")),
        })()

        class TicketRecorder:
            def __init__(self):
                self.created = []
            def create_or_get(self, _db, **kwargs):
                self.created.append(kwargs)
                return type("Ticket", (), {"id": 1, "ticket_number": "CS-TEST-000001"})()

        orchestrator = AgentOrchestrator()
        recorder = TicketRecorder()
        orchestrator.handoff_service = HandoffService(MemoryManager(), ticket_service=recorder)
        orchestrator.memory_manager = orchestrator.handoff_service.memory_manager
        orchestrator.intent_classifier = IntentClassifier(provider=provider)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        try:
            response = asyncio.run(
                orchestrator.handle("failure-session", "Apa kabar?", object(), "test-owner"),
            )
        finally:
            logger.removeHandler(handler)

        state = orchestrator.memory_manager.get_session("failure-session")
        self.assertIn("perlu diteruskan", response)
        self.assertEqual(state["handoff_state"]["category"], "internal_error")
        self.assertEqual(len(recorder.created), 1)
        self.assertEqual(provider.chat.await_count, 1)
        self.assertNotIn("private provider failure", stream.getvalue())

    def test_greeting_intent_does_not_leak_into_next_reservation_message(self):
        orchestrator = AgentOrchestrator()
        session_id = "greeting-then-reservation"
        owner_customer_id = "test-owner"

        greeting_response = asyncio.run(
            orchestrator.handle(
                session_id,
                "Halo",
                object(),
                owner_customer_id,
            )
        )

        reservation_response = asyncio.run(
            orchestrator.handle(
                session_id,
                "Saya mau membuat reservasi",
                object(),
                owner_customer_id,
            )
        )

        self.assertIn("Halo", greeting_response)
        self.assertIn("Atas nama siapa", reservation_response)
        self.assertEqual(
            orchestrator.memory_manager.get_session(session_id).get("intent"),
            "reservation",
        )

    def test_confirmation_phrase_outside_context_does_not_mutate_reservation(self):
        provider = type(
            "GeneralProvider",
            (),
            {
                "chat": AsyncMock(
                    return_value='{"intent":"general","confidence":0.9}'
                )
            },
        )()
        orchestrator = AgentOrchestrator()
        orchestrator.intent_classifier = IntentClassifier(provider=provider)
        session_id = "confirmation-outside-context"
        orchestrator.memory_manager.update_session(
            session_id,
            {
                "name": "Rizal",
                "people": 2,
                "date": "2026-07-30",
                "time": "19:00",
                "completed": False,
                "awaiting_confirmation": False,
            },
        )
        reservation_agent = orchestrator.workflow._agents["reservation"]
        reservation_agent.reservation_service.create_reservation = AsyncMock()

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "Ya lanjut",
                object(),
                "test-owner",
            )
        )

        self.assertIn("belum memahami", response)
        reservation_agent.reservation_service.create_reservation.assert_not_called()
        self.assertFalse(
            orchestrator.memory_manager.get_session(session_id).get(
                "awaiting_confirmation"
            )
        )

    def test_time_context_treats_malam_as_an_incomplete_time_not_greeting(self):
        orchestrator = AgentOrchestrator()
        session_id = "pending-time-not-greeting"
        orchestrator.memory_manager.update_session(
            session_id,
            {
                "intent": "reservation",
                "name": "Rizal",
                "people": 2,
                "date": "2026-07-30",
                "time": None,
                "completed": False,
                "awaiting_confirmation": False,
                "asked_fields": ["name", "people", "date", "time"],
            },
        )

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "malam",
                object(),
                "test-owner",
            )
        )

        session = orchestrator.memory_manager.get_session(session_id)
        self.assertEqual(response, "Jam berapa?")
        self.assertIsNone(session["time"])
        self.assertEqual(session["intent"], "reservation")

    def test_cancel_phrase_during_create_confirmation_rejects_create_only(self):
        orchestrator = AgentOrchestrator()
        session_id = "cancel-phrase-pending-create"
        orchestrator.memory_manager.update_session(
            session_id,
            {
                "intent": "reservation",
                "name": "Rizal",
                "people": 2,
                "date": "2026-07-30",
                "time": "19:00",
                "completed": False,
                "awaiting_confirmation": True,
                "asked_fields": ["name", "people", "date", "time"],
            },
        )
        reservation_agent = orchestrator.workflow._agents["reservation"]
        reservation_agent.reservation_service.create_reservation = AsyncMock()
        orchestrator.cancel_reservation_agent.run = AsyncMock()

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "tolong cancel booking saya",
                object(),
                "test-owner",
            )
        )

        session = orchestrator.memory_manager.get_session(session_id)
        self.assertEqual(response, "Baik, reservasi tidak dilanjutkan.")
        self.assertFalse(session.get("awaiting_confirmation", False))
        self.assertIsNone(session["intent"])
        reservation_agent.reservation_service.create_reservation.assert_not_called()
        orchestrator.cancel_reservation_agent.run.assert_not_awaited()

    def test_negative_phrase_outside_confirmation_preserves_unrelated_state(self):
        provider = type(
            "GeneralProvider",
            (),
            {
                "chat": AsyncMock(
                    return_value='{"intent":"general","confidence":0.9}'
                )
            },
        )()
        orchestrator = AgentOrchestrator()
        orchestrator.intent_classifier = IntentClassifier(provider=provider)
        orchestrator.ai = type(
            "GeneralAI",
            (),
            {"chat": AsyncMock(return_value="Silakan jelaskan kebutuhan Anda.")},
        )()
        session_id = "negative-outside-confirmation"
        orchestrator.memory_manager.update_session(
            session_id,
            {
                "awaiting_confirmation": False,
                "handoff_attempts": {"misunderstanding": 7},
            },
        )

        response = asyncio.run(
            orchestrator.handle(
                session_id,
                "nggak jadi",
                object(),
                "test-owner",
            )
        )

        session = orchestrator.memory_manager.get_session(session_id)
        self.assertIn("belum memahami", response)
        self.assertEqual(session["handoff_attempts"], {"misunderstanding": 7})
        self.assertFalse(session.get("awaiting_confirmation", False))
        self.assertFalse(orchestrator.handoff_service.is_required(session_id))

    def test_vague_help_repetition_does_not_create_handoff(self):
        for index, message in enumerate(
            ("tolong dong", "saya bingung", "bagaimana ya?", "bisa bantu?")
        ):
            with self.subTest(message=message):
                provider = type(
                    "GeneralProvider",
                    (),
                    {
                        "chat": AsyncMock(
                            return_value='{"intent":"general","confidence":0.0}'
                        )
                    },
                )()
                orchestrator = AgentOrchestrator()
                orchestrator.intent_classifier = IntentClassifier(
                    provider=provider
                )
                orchestrator.ai = type(
                    "GeneralAI",
                    (),
                    {"chat": AsyncMock(return_value="Silakan jelaskan.")},
                )()
                session_id = f"vague-help-{index}"

                for _ in range(2):
                    response = asyncio.run(
                        orchestrator.handle(
                            session_id,
                            message,
                            object(),
                            "test-owner",
                        )
                    )
                    self.assertEqual(response, "Silakan jelaskan.")

                self.assertFalse(
                    orchestrator.handoff_service.is_required(session_id)
                )


if __name__ == "__main__":
    unittest.main()
