import asyncio
import unittest

from app.agents.orchestrator import AgentOrchestrator
from app.agents.workflow import AgentWorkflow
from app.brain.planner import Planner


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


if __name__ == "__main__":
    unittest.main()
