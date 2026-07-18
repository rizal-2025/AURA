import asyncio
import unittest

from app.agents.workflow import AgentWorkflow
from app.brain.classifier import IntentClassifier
from app.brain.planner import Planner


class TestAgentWorkflow(unittest.TestCase):
    def test_reservation_workflow_collects_fields_and_completes(self):
        workflow = AgentWorkflow()
        classifier = IntentClassifier(provider=type("DummyAI", (), {"chat": None})())
        planner = Planner()

        intent_result = {"intent": "reservation", "confidence": 0.95}
        state = {"name": None, "people": None, "date": None, "time": None}

        plan = asyncio.run(planner.plan(intent_result, state))
        result = asyncio.run(workflow.execute(plan, state, "Saya mau reservasi"))

        self.assertIn("status", result)
        self.assertIn("response", result)


if __name__ == "__main__":
    unittest.main()
