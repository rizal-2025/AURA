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


if __name__ == "__main__":
    unittest.main()
