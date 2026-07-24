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

    def test_comma_delimited_name_before_people_clause(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(
            extractor.extract(
                "Buat reservasi atas nama Rizal, untuk 4 orang besok jam 7 malam"
            )
        )

        self.assertEqual(result["name"], "Rizal")
        self.assertEqual(result["people"], 4)

    def test_multiword_name_before_date_and_time_clause(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(
            extractor.extract(
                "Atas nama Ahmad Rizal, tanggal besok jam 7 malam"
            )
        )

        self.assertEqual(result["name"], "Ahmad Rizal")

    def test_valid_name_punctuation_is_preserved(self):
        extractor = ReservationEntityExtractor()
        names = (
            "A.J.",
            "D'Angelo",
            "O\u2019Connor",
            "Smith-Jones",
            "R&D",
            "Siti Hari",
        )
        for name in names:
            with self.subTest(name=name):
                result = asyncio.run(
                    extractor.extract(f"Atas nama {name}, untuk 4 orang")
                )
                self.assertEqual(result["name"], name)

    def test_trailing_sentence_delimiter_policy_preserves_period(self):
        extractor = ReservationEntityExtractor()
        period = asyncio.run(extractor.extract("Atas nama A.J."))
        exclamation = asyncio.run(extractor.extract("Atas nama Rizal!"))

        self.assertEqual(period["name"], "A.J.")
        self.assertEqual(exclamation["name"], "Rizal")

    def test_invalid_name_punctuation_remains_rejected(self):
        extractor = ReservationEntityExtractor()
        for name in ("Bad/Name", "Bad_Name"):
            with self.subTest(name=name):
                result = asyncio.run(extractor.extract(f"Atas nama {name}"))
                self.assertNotIn("name", result)

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
