import unittest

from app.brain.memory_manager import MemoryManager


class TestMemoryManager(unittest.TestCase):
    def test_create_and_update_session(self):
        manager = MemoryManager()
        session = manager.create_session("s1")

        self.assertEqual(session["completed"], False)

        manager.update_session("s1", {"intent": "reservation"})
        state = manager.get_session("s1")

        self.assertEqual(state["intent"], "reservation")

    def test_session_defaults_are_initialized(self):
        manager = MemoryManager()
        state = manager.get_session("s2")

        self.assertIn("intent", state)
        self.assertIn("name", state)
        self.assertIn("people", state)
        self.assertIn("date", state)
        self.assertIn("time", state)
        self.assertEqual(state["completed"], False)


if __name__ == "__main__":
    unittest.main()
