"""Permanent independent contracts for the four pre-merge counterexamples."""
from datetime import date
import unittest

from app.utils.datetime_parser import DatetimeParser, MONTHS

AMBIGUOUS_DATES = (
    "06-09-2026 atau 07-09-2026", "6 September atau 7 September",
    "6 September / 7 September", "antara 6 September dan 7 September",
    "6 September, bukan 7 September", "7 September, bukan 6 September",
)
MALFORMED_DATES = (
    "September 6 20266", "6 September 20266", "6/9/20266",
    "6-9-20266", "September 6 20x6", "September 6 026",
)
COMBINED_DATES = (
    "6 September 20:00", "6 September jam 20:00",
    "6 September pukul 20:00", "6 September 8 malam",
    "tanggal 6 September 20:00",
)


def generated_parser_cases():
    """Unique semantic inputs, not repeated executions or random identities."""
    cases = {}
    for day in range(1, 32):
        for month, number in MONTHS.items():
            for year in ("2026", "2028", "20266", "20x6", "026", "0000"):
                try:
                    expected = date(int(year), number, day).isoformat() if year in ("2026", "2028") else None
                except ValueError:
                    expected = None
                for text in (f"{day} {month} {year}", f"{month} {day} {year}"):
                    cases[("date", text)] = expected
    for day in range(1, 29):
        for separator in ("/", "-"):
            for year in ("20266", "20x6", "026", "0000"):
                cases[("date", f"{day}{separator}9{separator}{year}")] = None
        for joiner in (" atau ", " / ", " dan ", ", bukan "):
            for left, right in (
                (f"{day}-09-2027", f"{day+1}-09-2027"),
                (f"{day} September", f"{day+1} September"),
                (f"September {day}", f"September {day+1}"),
            ):
                cases[("date", left + joiner + right)] = None
    for prefix in ("", "at ", "booking at ", "I am booking at ", "I am reserving for "):
        for hour in range(1, 13):
            for period in ("am", "pm"):
                for spacing in ("", " "):
                    expected = f"{hour % 12 + (12 if period == 'pm' else 0):02}:00"
                    cases[("time", f"{prefix}{hour}{spacing}{period}")] = expected
    return cases


class BlockerParserTests(unittest.TestCase):
    def test_ambiguous_two_date(self):
        for text in AMBIGUOUS_DATES:
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))

    def test_malformed_attached_year(self):
        for text in MALFORMED_DATES:
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))

    def test_combined_date_time_preserves_both_components(self):
        for text in COMBINED_DATES:
            with self.subTest(text=text):
                self.assertEqual(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)), "2026-09-06")
                self.assertEqual(DatetimeParser.parse_time(text), "20:00")

    def test_english_grammatical_am_is_not_a_clock(self):
        for prefix in ("", "at ", "booking at ", "I am booking at ", "I am reserving for "):
            for text, expected in (("11 pm", "23:00"), ("11pm", "23:00"), ("11 am", "11:00")):
                with self.subTest(text=prefix+text):
                    self.assertEqual(DatetimeParser.parse_time(prefix+text), expected)
        for text in ("I am booking", "I am Dani", "I am booking tomorrow"):
            self.assertIsNone(DatetimeParser.parse_time(text))
        self.assertIsNone(DatetimeParser.parse_time("11 am pm"))
        self.assertIsNone(DatetimeParser.parse_time("11 am or 11 pm"))

    def test_generated_properties(self):
        for (kind, text), expected in generated_parser_cases().items():
            with self.subTest(kind=kind, text=text):
                actual = (DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5))
                          if kind == "date" else DatetimeParser.parse_time(text))
                self.assertEqual(actual, expected)

    def test_unrelated_numbers_and_partial_grammar(self):
        self.assertEqual(DatetimeParser.parse_date("6 September, catatan 123456789", reference_date=date(2026, 9, 5)), "2026-09-06")
        self.assertEqual(DatetimeParser.parse_date("September 6, catatan 123456789", reference_date=date(2026, 9, 5)), "2026-09-06")
        for text in ("September 2026", "tanggal 5", "tahun 2027", "bulan depan tanggal 5"):
            self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))
        for text in ("besok 6/9/20266", "6-9-20x6 atau 7 September 2026"):
            self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))

    def test_year_boundary_contract_is_unchanged(self):
        for now, text, expected in (
            (date(2026, 9, 5), "4 September", "2027-09-04"),
            (date(2026, 9, 5), "5 September", "2026-09-05"),
            (date(2026, 12, 31), "1 January", "2027-01-01"),
            (date(2026, 12, 31), "31 December", "2026-12-31"),
            (date(2027, 1, 1), "1 January", "2027-01-01"),
        ):
            with self.subTest(now=now, text=text):
                self.assertEqual(DatetimeParser.parse_date(text, reference_date=now), expected)
