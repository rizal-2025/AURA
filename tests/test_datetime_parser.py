import unittest
from datetime import date

from app.utils.datetime_parser import DatetimeParser


class TestDatetimeParser(unittest.TestCase):
    def setUp(self):
        self.reference_date = date(2026, 7, 18)

    def test_besok(self):
        self.assertEqual(
            DatetimeParser.parse_date("besok", reference_date=self.reference_date),
            "2026-07-19",
        )

    def test_lusa(self):
        self.assertEqual(
            DatetimeParser.parse_date("lusa", reference_date=self.reference_date),
            "2026-07-20",
        )

    def test_jumat(self):
        self.assertEqual(
            DatetimeParser.parse_date(
                "hari Jumat",
                reference_date=self.reference_date,
            ),
            "2026-07-24",
        )

    def test_time_7_malam(self):
        self.assertEqual(DatetimeParser.parse_time("jam 7 malam"), "19:00")

    def test_time_7_pagi(self):
        self.assertEqual(DatetimeParser.parse_time("jam 7 pagi"), "07:00")

    def test_time_setengah_delapan_malam(self):
        self.assertEqual(DatetimeParser.parse_time("setengah delapan malam"), "19:30")

    def test_time_12_siang(self):
        self.assertEqual(DatetimeParser.parse_time("jam 12 siang"), "12:00")

    def test_time_12_malam(self):
        self.assertEqual(DatetimeParser.parse_time("jam 12 malam"), "00:00")

    def test_qualified_times_do_not_require_clock_prefix(self):
        for value, expected in (
            ("8 pagi", "08:00"),
            ("10 pagi", "10:00"),
            ("jam 8 pagi", "08:00"),
            ("jam 10 pagi", "10:00"),
            ("2 siang", "14:00"),
            ("3 sore", "15:00"),
            ("7 malam", "19:00"),
            ("12 siang", "12:00"),
            ("12 malam", "00:00"),
        ):
            with self.subTest(value=value):
                self.assertEqual(DatetimeParser.parse_time(value), expected)

    def test_existing_explicit_and_ambiguous_boundaries_remain(self):
        for value, expected in (
            ("08:00", "08:00"),
            ("8:00", "08:00"),
            ("8.00", "08:00"),
            ("00:00", "00:00"),
        ):
            with self.subTest(value=value):
                self.assertEqual(DatetimeParser.parse_time(value), expected)
        self.assertIsNone(DatetimeParser.parse_time("24:00"))
        self.assertIsNone(DatetimeParser.parse_time("jam 8"))


if __name__ == "__main__":
    unittest.main()
