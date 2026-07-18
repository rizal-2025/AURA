import unittest

from app.brain.conversation_state_manager import ConversationStateManager


class TestConversationStateManager(unittest.TestCase):
    def setUp(self):
        self.manager = ConversationStateManager()

    def test_initial_state_returns_ask_name(self):
        state = {}
        result = self.manager.get_next_action(state)

        self.assertEqual(result["next_action"], "ask_name")
        self.assertEqual(result["field"], "name")
        self.assertEqual(result["question"], "Atas nama siapa reservasinya?")

    def test_skips_already_asked_field(self):
        state = {"asked_fields": ["name"]}
        result = self.manager.get_next_action(state)

        self.assertEqual(result["next_action"], "ask_people")
        self.assertEqual(result["field"], "people")

    def test_returns_confirm_when_all_fields_present(self):
        state = {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
        }
        result = self.manager.get_next_action(state)

        self.assertEqual(result["next_action"], "confirm")
        self.assertEqual(result["field"], None)

    def test_marks_question_to_avoid_repetition(self):
        state = {}
        updated = self.manager.record_question(state, "name")

        self.assertIn("name", updated["asked_fields"])

    def test_returns_complete_when_flow_is_already_finished(self):
        state = {"completed": True}
        result = self.manager.get_next_action(state)

        self.assertEqual(result["next_action"], "complete")
