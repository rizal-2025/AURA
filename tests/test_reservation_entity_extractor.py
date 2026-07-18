import asyncio
import unittest

from app.brain.reservation_entity_extractor import ReservationEntityExtractor
from app.agents.reservation_agent import ReservationAgent


class TestReservationEntityExtractor(unittest.TestCase):
    def test_extracts_complete_entities(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(
            extractor.extract("Saya ingin reservasi besok jam 7 malam untuk 4 orang atas nama Rizal")
        )

        self.assertEqual(result["name"], "Rizal")
        self.assertEqual(result["people"], 4)
        self.assertEqual(result["date"], "besok")
        self.assertEqual(result["time"], "19:00")

    def test_extracts_partial_entities(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(extractor.extract("Besok jam 7"))

        self.assertEqual(result["date"], "besok")
        self.assertEqual(result["time"], "19:00")

    def test_returns_empty_when_no_entities(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(extractor.extract("Halo apa kabar"))

        self.assertEqual(result, {})

    def test_agent_uses_extractor_and_memory(self):
        agent = ReservationAgent()
        result = asyncio.run(
            agent.run(
                [{"action": "collect_missing_fields", "fields": ["name", "people", "date", "time"]}],
                {"name": None, "people": None, "date": None, "time": None},
                "Saya ingin reservasi besok jam 7 malam untuk 4 orang atas nama Rizal",
            )
        )

        self.assertIn("status", result)


if __name__ == "__main__":
    unittest.main()
