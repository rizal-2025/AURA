import unittest

from app.memory.long_term_memory import LongTermMemoryManager


class TestLongTermMemoryManager(unittest.TestCase):
    def test_saves_and_returns_profile(self):
        manager = LongTermMemoryManager()

        manager.merge_preferences("user-1", {
            "favorite_name": "Rizal",
            "preferred_people": 4,
            "favorite_time": "19:00",
            "favorite_table": "A-2",
        })

        profile = manager.get_profile("user-1")

        self.assertEqual(profile["favorite_name"], "Rizal")
        self.assertEqual(profile["preferred_people"], 4)
        self.assertEqual(profile["favorite_time"], "19:00")
        self.assertEqual(profile["favorite_table"], "A-2")

    def test_suggests_context_for_existing_user(self):
        manager = LongTermMemoryManager()
        manager.merge_preferences("user-2", {"favorite_name": "Budi", "favorite_time": "20:00"})

        suggestion = manager.suggest_context("user-2")

        self.assertEqual(suggestion["favorite_name"], "Budi")
        self.assertEqual(suggestion["favorite_time"], "20:00")

    def test_clear_profile_removes_preferences(self):
        manager = LongTermMemoryManager()
        manager.merge_preferences("user-3", {"favorite_name": "Nina"})
        manager.clear_profile("user-3")

        self.assertEqual(manager.get_profile("user-3"), {})
