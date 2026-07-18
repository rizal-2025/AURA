import asyncio
import unittest

from app.brain.planner import Planner


class TestBrainPlanner(unittest.TestCase):
    def test_returns_steps_for_reservation_intent(self):
        planner = Planner()
        result = asyncio.run(
            planner.plan(
                intent_result={"intent": "reservation", "confidence": 0.95},
                conversation_state={"name": None, "people": None, "date": None, "time": None},
            )
        )

        self.assertIn("steps", result)
        self.assertTrue(isinstance(result["steps"], list))
        self.assertGreaterEqual(len(result["steps"]), 1)

    def test_returns_general_steps_for_unknown_intent(self):
        planner = Planner()
        result = asyncio.run(
            planner.plan(
                intent_result={"intent": "general", "confidence": 0.1},
                conversation_state={},
            )
        )

        self.assertEqual(result["intent"], "general")
        self.assertTrue(result["steps"])


if __name__ == "__main__":
    unittest.main()
