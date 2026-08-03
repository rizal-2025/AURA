import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.brain.reservation_entity_extractor import (
    PublicReferenceParseStatus,
    ReservationEntityExtractor,
    parse_public_reservation_reference,
)
from app.agents.reservation_agent import ReservationAgent


class TestReservationEntityExtractor(unittest.TestCase):
    @staticmethod
    def _extractor():
        return ReservationEntityExtractor(
            clock=lambda: datetime(
                2026,
                7,
                18,
                23,
                0,
                tzinfo=ZoneInfo("Asia/Jakarta"),
            )
        )

    def test_extracts_complete_entities(self):
        extractor = self._extractor()
        result = asyncio.run(
            extractor.extract("Saya ingin reservasi besok jam 7 malam untuk 4 orang atas nama Rizal")
        )

        self.assertEqual(result["name"], "Rizal")
        self.assertEqual(result["people"], 4)
        self.assertEqual(result["date"], "2026-07-19")
        self.assertEqual(result["time"], "19:00")

    def test_extracts_partial_entities(self):
        extractor = self._extractor()
        result = asyncio.run(extractor.extract("Besok jam 7"))

        self.assertEqual(result["date"], "2026-07-19")
        self.assertNotIn("time", result)

    def test_returns_empty_when_no_entities(self):
        extractor = ReservationEntityExtractor()
        result = asyncio.run(extractor.extract("Halo apa kabar"))

        self.assertEqual(result, {})

    def test_public_reference_selection_is_bounded_and_canonical(self):
        canonical = "RSV_" + "ab" * 16
        mixed = "rSv_" + "Ab" * 16
        for message in (
            mixed,
            f"referensi reservasi: {mixed}",
            f"reservasi {mixed}",
            f"pilih {mixed}",
            f"gunakan referensi {mixed}.",
        ):
            with self.subTest(message=message):
                parsed = parse_public_reservation_reference(message)
                self.assertIs(parsed.status, PublicReferenceParseStatus.VALID)
                self.assertEqual(parsed.reference, canonical)

    def test_public_reference_accepts_one_matching_outer_pair_for_each_wrapper(self):
        canonical = "RSV_" + "ab" * 16
        mixed = "rSv_" + "Ab" * 16
        wrappers = (
            mixed,
            f"referensi reservasi: {mixed}",
            f"reservasi {mixed}",
            f"pilih {mixed}",
            f"gunakan referensi {mixed}",
        )
        pairs = (
            ("(", ")"),
            ("[", "]"),
            ('"', '"'),
            ("'", "'"),
            ("\u201c", "\u201d"),
            ("\u2018", "\u2019"),
        )

        for wrapper in wrappers:
            for opener, closer in pairs:
                for terminal in ("", ".", "!", "?", ",", ";"):
                    message = f" {opener} {wrapper}{terminal} {closer} "
                    with self.subTest(
                        wrapper=wrapper,
                        pair=opener + closer,
                        terminal=terminal,
                    ):
                        parsed = parse_public_reservation_reference(message)
                        self.assertIs(parsed.status, PublicReferenceParseStatus.VALID)
                        self.assertEqual(parsed.reference, canonical)

    def test_public_reference_rejects_mismatched_nested_and_partial_outer_pairs(self):
        valid = "RSV_" + "ab" * 16
        malformed = (
            f"({valid}]",
            f"[{valid})",
            f"\u201c{valid}\u2019",
            f"\u2018{valid}\u201d",
            f"({valid}",
            f"{valid})",
            f"[{valid}",
            f"{valid}]",
            f'"{valid}',
            f'{valid}"',
            f"'{valid}",
            f"{valid}'",
            f"(({valid}))",
            f"[[{valid}]]",
            f'""{valid}""',
            f"''{valid}''",
            f"([{valid}])",
            f"prefix ({valid})",
            f"({valid}) suffix",
            f"({valid}) dan lanjut",
            f"({valid[:-1]}.{valid[-1]})",
        )

        for message in malformed:
            with self.subTest(message=message):
                parsed = parse_public_reservation_reference(message)
                self.assertIs(parsed.status, PublicReferenceParseStatus.MALFORMED)
                self.assertIsNone(parsed.reference)

    def test_public_reference_rejects_adjacent_word_and_control_boundaries(self):
        valid = "RSV_" + "ab" * 16
        malformed = (
            f"A{valid}",
            f"{valid}Z",
            f"7{valid}",
            f"{valid}8",
            f"_{valid}",
            f"{valid}_",
            f"\u00e9{valid}",
            f"{valid}\u00e9",
            f"\u0661{valid}",
            f"{valid}\u0661",
            f"\t{valid}",
            f"{valid}\t",
            f"\r{valid}",
            f"{valid}\n",
            f"{valid}\r\n",
            f"reservasi\t{valid}",
            f"gunakan\nreferensi {valid}",
            f"({valid}\n)",
        )

        for message in malformed:
            with self.subTest(message=message):
                parsed = parse_public_reservation_reference(message)
                self.assertIs(parsed.status, PublicReferenceParseStatus.MALFORMED)

    def test_public_reference_precedence_is_deterministic(self):
        first = "RSV_" + "ab" * 16
        second = "RSV_" + "cd" * 16
        malformed = "RSV_" + "g" * 32
        cases = (
            (f"{first} dan {second}", PublicReferenceParseStatus.AMBIGUOUS),
            (f"({first}) {second}", PublicReferenceParseStatus.AMBIGUOUS),
            (f"{first} {malformed}", PublicReferenceParseStatus.MALFORMED),
            (f"{malformed} {first}", PublicReferenceParseStatus.MALFORMED),
            (f"{malformed} {malformed}", PublicReferenceParseStatus.MALFORMED),
            (f"{first} {first}", PublicReferenceParseStatus.AMBIGUOUS),
            ("123456", PublicReferenceParseStatus.MALFORMED),
            ("arbitrary-id", PublicReferenceParseStatus.MALFORMED),
            ("belum punya referensi", PublicReferenceParseStatus.MISSING),
        )

        for message, status in cases:
            with self.subTest(message=message):
                parsed = parse_public_reservation_reference(message)
                self.assertIs(parsed.status, status)
                self.assertIsNone(parsed.reference)

    def test_public_reference_parser_rejects_unsafe_selection(self):
        valid = "RSV_" + "ab" * 16
        cases = {
            "": PublicReferenceParseStatus.MISSING,
            "referensi reservasi belum ada": PublicReferenceParseStatus.MISSING,
            "2": PublicReferenceParseStatus.MALFORMED,
            "ABC-123": PublicReferenceParseStatus.MALFORMED,
            "RSV_abc": PublicReferenceParseStatus.MALFORMED,
            "RSV_" + "g" * 32: PublicReferenceParseStatus.MALFORMED,
            f"prefix{valid}": PublicReferenceParseStatus.MALFORMED,
            f"gunakan {valid}": PublicReferenceParseStatus.MALFORMED,
            f"{valid} dan {valid}": PublicReferenceParseStatus.AMBIGUOUS,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                parsed = parse_public_reservation_reference(message)
                self.assertIs(parsed.status, expected)
                self.assertIsNone(parsed.reference)
                if message:
                    self.assertNotIn(message, repr(parsed))

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
