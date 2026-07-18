import unittest

from app.brain.context_resolver import ContextResolver


class TestContextResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = ContextResolver()

    def test_updates_only_time_for_change_request(self):
        state = {"name": "Rizal", "people": 4, "date": "2026-07-18", "time": "19:00"}
        extracted = {"time": "20:00"}

        updated = self.resolver.resolve(state, "Eh ganti jadi jam 8 saja.", extracted)

        self.assertEqual(updated["time"], "20:00")
        self.assertEqual(updated["name"], "Rizal")
        self.assertEqual(updated["people"], 4)
        self.assertEqual(updated["date"], "2026-07-18")

    def test_updates_multiple_fields_when_not_a_change_request(self):
        state = {"name": "Rizal", "people": 4, "date": "2026-07-18", "time": "19:00"}
        extracted = {"name": "Budi", "time": "20:00"}

        updated = self.resolver.resolve(state, "Nama saya Budi, jam 8 malam.", extracted)

        self.assertEqual(updated["name"], "Budi")
        self.assertEqual(updated["time"], "20:00")

    def test_ignores_empty_extraction(self):
        state = {"name": "Rizal", "people": 4, "date": "2026-07-18", "time": "19:00"}
        updated = self.resolver.resolve(state, "Eh ganti jadi jam 8 saja.", {})

        self.assertEqual(updated, state)
