import asyncio
import json
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.reservation_agent import (
    CONFIRM,
    EDIT_FIELD,
    REJECT,
    ReservationAgent,
)
from app.agents.stub_agents import GreetingAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.classifier import IntentClassifier
from app.brain.indonesian_nlu import (
    normalize_indonesian_text,
    parse_confirmation,
    parse_people_count,
    parse_target_field,
)
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_entity_extractor import ReservationEntityExtractor
from app.services.handoff.detector import HandoffDetector
from app.utils.datetime_parser import DatetimeParser


JAKARTA = ZoneInfo("Asia/Jakarta")
FROZEN_NOW = datetime(2026, 7, 26, 10, 0, tzinfo=JAKARTA)


class IndonesianNormalizationTests(unittest.TestCase):
    def test_informal_negatives_normalize_as_complete_tokens(self):
        for value in ("gak", "ga", "nggak", "ngga", "enggak", "tidak"):
            with self.subTest(value=value):
                self.assertEqual(normalize_indonesian_text(value), "tidak")

    def test_booking_and_preference_synonyms_normalize(self):
        self.assertEqual(
            normalize_indonesian_text("Mo pesen meja malem"),
            "ingin pesan meja malam",
        )
        self.assertEqual(
            normalize_indonesian_text("Pengen booking"),
            "ingin reservasi",
        )

    def test_punctuation_and_whitespace_are_bounded(self):
        self.assertEqual(
            normalize_indonesian_text("  Mau,   BOOKING!!!\n"),
            "ingin reservasi",
        )

    def test_name_substrings_are_not_corrupted(self):
        self.assertEqual(
            normalize_indonesian_text("atas nama Gandi Bookingan"),
            "atas nama gandi bookingan",
        )

    def test_unknown_words_remain_unchanged(self):
        self.assertEqual(
            normalize_indonesian_text("kuliner nyaman"),
            "kuliner nyaman",
        )


class IndonesianIntentTests(unittest.TestCase):
    def test_natural_greetings(self):
        for message in (
            "Halo",
            "Hai",
            "Halo min",
            "Hai min",
            "Pagi",
            "Selamat pagi",
            "Selamat siang",
            "Selamat sore",
            "Selamat malam!",
            "Permisi",
            "Halo Aura",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_greeting_intent(message),
                    "greeting",
                )

    def test_natural_intent_vocabulary_matrix(self):
        cases = {
            "tolong booking meja": "reservation",
            "ada meja untuk besok?": "reservation",
            "tolong siapkan meja": "reservation",
            "booking saya perlu direvisi": "update_reservation",
            "saya mau pindah jadwal": "update_reservation",
            "reservasinya mau diedit": "update_reservation",
            "ada perubahan untuk booking saya": "update_reservation",
            "jadwalnya digeser": "update_reservation",
            "reservasinya tidak jadi": "cancel_reservation",
            "saya mau membatalkan pesanan": "cancel_reservation",
            "nggak jadi pakai bookingnya": "cancel_reservation",
            "pesanan saya masuk belum?": "view_reservation",
            "cek jadwal saya": "view_reservation",
            "nomor reservasi saya berapa?": "view_reservation",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    expected,
                )

    def test_similar_but_irrelevant_text_remains_non_actionable(self):
        for message in (
            "ubah",
            "ganti",
            "ada meja?",
            "meja tersedia?",
            "jadwal restoran",
            "nomor meja berapa?",
            "beberapa teman",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    IntentClassifier.detect_reservation_intent(message)
                )

    def test_create_variants(self):
        for message in (
            "Mau booking meja",
            "Saya ingin pesan meja untuk besok malam",
            "Mau reservasi berdua atas nama Rizal",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    "reservation",
                )

    def test_update_variants(self):
        for message in (
            "Tolong ubah jadwal reservasi saya",
            "Rubah tanggal booking saya",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    "update_reservation",
                )

    def test_cancellation_variants(self):
        for message in (
            "Batalin booking saya",
            "Saya nggak jadi datang",
            "Hapus reservasi saya",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    "cancel_reservation",
                )

    def test_status_variants(self):
        for message in (
            "Cek reservasi saya",
            "Booking saya masih aktif?",
            "Tampilkan pesanan meja saya",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    "view_reservation",
                )

    def test_handoff_variants_are_explicit(self):
        for message in (
            "Saya mau bicara dengan admin",
            "Hubungkan saya ke admin",
            "Saya mau ngomong sama petugas",
            "Bisa sambungkan ke manusia?",
            "Saya butuh bantuan orang",
            "Saya mau dilayani langsung",
            "Tolong panggil admin",
            "Mau chat dengan customer service",
            "Saya perlu bantuan staf",
            "Bisa bicara dengan operator?",
        ):
            with self.subTest(message=message):
                self.assertTrue(HandoffDetector.is_explicit_human_request(message))

    def test_vague_help_is_safe_non_action_text_not_explicit_handoff(self):
        for message in (
            "tolong dong",
            "saya bingung",
            "bagaimana ya?",
            "bisa bantu?",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    HandoffDetector.is_explicit_human_request(message)
                )
                self.assertFalse(
                    HandoffDetector.is_deterministically_misunderstood(message)
                )
                self.assertTrue(HandoffDetector.is_safe_non_action_message(message))

    def test_low_confidence_text_does_not_trigger_action_or_handoff(self):
        message = "Tempatnya terasa nyaman"
        self.assertIsNone(IntentClassifier.detect_reservation_intent(message))
        self.assertFalse(HandoffDetector.is_explicit_human_request(message))


class IndonesianPeopleParsingTests(unittest.TestCase):
    def test_numeric_and_number_word_counts(self):
        for message, expected in (
            ("2", 2),
            ("dua", 2),
            ("dua orang", 2),
            ("buat 5 orang", 5),
            ("meja untuk enam", 6),
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_people_count(message), expected)

    def test_group_forms(self):
        for message, expected in (
            ("berdua", 2),
            ("bertiga", 3),
            ("kami berempat", 4),
            ("kami berlima", 5),
            ("cuma berdua", 2),
            ("hanya berdua", 2),
            ("hanya bertiga", 3),
            ("total tujuh orang", 7),
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_people_count(message), expected)

    def test_contextual_people_correction(self):
        self.assertEqual(parse_people_count("orangnya jadi tiga"), 3)

    def test_word_counts_with_natural_carriers(self):
        self.assertEqual(parse_people_count("buat empat orang"), 4)
        self.assertEqual(parse_people_count("meja untuk enam"), 6)

    def test_invalid_and_oversized_counts_fail_closed(self):
        for message in (
            "0",
            "-2",
            "21",
            "3.5",
            "2 atau 3 orang",
            "beberapa teman",
            "true",
        ):
            with self.subTest(message=message):
                self.assertIsNone(parse_people_count(message))


class IndonesianDateParsingTests(unittest.TestCase):
    def test_explicit_date_formats(self):
        for message in (
            "30 Juli 2026",
            "30 juli 2026",
            "30/07/2026",
            "30-07-2026",
            "2026-07-30",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    DatetimeParser.parse_date(
                        message,
                        reference_date=date(2026, 7, 26),
                    ),
                    "2026-07-30",
                )

    def test_optional_year_uses_nearest_non_past_occurrence(self):
        self.assertEqual(
            DatetimeParser.parse_date(
                "30 juli",
                reference_date=date(2026, 7, 26),
            ),
            "2026-07-30",
        )
        self.assertEqual(
            DatetimeParser.parse_date(
                "1 januari",
                reference_date=date(2026, 7, 26),
            ),
            "2027-01-01",
        )

    def test_relative_and_next_weekday_dates(self):
        for message, expected in (
            ("besok", "2026-07-27"),
            ("lusa", "2026-07-28"),
            ("Jumat depan", "2026-07-31"),
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    DatetimeParser.parse_date(
                        message,
                        reference_date=date(2026, 7, 26),
                    ),
                    expected,
                )

    def test_additional_weekday_variants(self):
        reference = date(2026, 7, 24)
        for message, expected in (
            ("minggu depan", "2026-07-26"),
            ("hari Jumat", "2026-07-31"),
            ("Jumat ini", "2026-07-24"),
            ("Jumat depan", "2026-07-31"),
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    DatetimeParser.parse_date(
                        message,
                        reference_date=reference,
                    ),
                    expected,
                )

    def test_jumat_ini_after_weekday_has_passed_is_not_guessed(self):
        self.assertIsNone(
            DatetimeParser.parse_date(
                "Jumat ini",
                reference_date=date(2026, 7, 25),
            )
        )

    def test_impossible_unknown_and_ambiguous_dates_fail_closed(self):
        for message in (
            "30 Februari 2026",
            "30 Smarch 2026",
            "tanggal 5",
            "akhir bulan",
            "bulan depan saja",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    DatetimeParser.parse_date(
                        message,
                        reference_date=date(2026, 7, 26),
                    )
                )
        self.assertEqual(
            DatetimeParser.date_ambiguity("tanggal 5"),
            "missing_month_year",
        )

    def test_injected_clock_uses_asia_jakarta_date_boundary(self):
        utc_clock = lambda: datetime(2026, 7, 26, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(
            DatetimeParser.parse_date("besok", clock=utc_clock),
            "2026-07-28",
        )


class IndonesianTimeParsingTests(unittest.TestCase):
    def test_canonical_and_dot_times(self):
        self.assertEqual(DatetimeParser.parse_time("19:00"), "19:00")
        self.assertEqual(DatetimeParser.parse_time("19.00"), "19:00")

    def test_qualified_numeric_and_word_times(self):
        for message, expected in (
            ("jam 7 malam", "19:00"),
            ("pukul tujuh malam", "19:00"),
            ("jam 7 pagi", "07:00"),
            ("jam 7 sore", "19:00"),
            ("jam 7 siang", "19:00"),
        ):
            with self.subTest(message=message):
                self.assertEqual(DatetimeParser.parse_time(message), expected)

    def test_half_hour_expression(self):
        self.assertEqual(
            DatetimeParser.parse_time("setengah delapan malam"),
            "19:30",
        )

    def test_around_and_minute_offset_expressions(self):
        for message, expected in (
            ("sekitar jam tujuh malam", "19:00"),
            ("jam delapan lewat lima belas", "08:15"),
            ("jam tujuh lewat tiga puluh", "07:30"),
            ("sekitar pukul delapan pagi", "08:00"),
        ):
            with self.subTest(message=message):
                self.assertEqual(DatetimeParser.parse_time(message), expected)

    def test_ambiguous_and_invalid_times_fail_closed(self):
        self.assertIsNone(DatetimeParser.parse_time("jam 7"))
        self.assertEqual(
            DatetimeParser.time_ambiguity("jam 7"),
            "missing_day_period",
        )
        for message in (
            "25:00",
            "jam 14 malam",
            "setengah delapan",
            "nanti malam",
            "agak sore",
            "habis magrib",
        ):
            with self.subTest(message=message):
                self.assertIsNone(DatetimeParser.parse_time(message))


class ContextAwareUnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryManager()
        self.agent = ReservationAgent(
            memory_manager=self.memory,
            clock=lambda: FROZEN_NOW,
        )

    def _run_pending(self, field, message, known):
        required = ["name", "people", "date", "time"]
        state = {
            "intent": "reservation",
            "completed": False,
            "awaiting_confirmation": False,
            "asked_fields": required[: required.index(field) + 1],
            **{item: known.get(item) for item in required},
        }
        return asyncio.run(
            self.agent.run(
                [{"action": "collect_missing_fields"}],
                state,
                message,
                session_id=f"pending-{field}",
            )
        )

    def test_people_answer_wins_in_people_context(self):
        result = self._run_pending(
            "people",
            "kami bertiga",
            {"name": "Rizal"},
        )
        self.assertEqual(
            self.memory.get_session("pending-people")["people"],
            3,
        )
        self.assertEqual(result["field"], "date")

    def test_confirmation_words_inside_pending_name_are_preserved_as_name(self):
        for message in ("Sip Lanjut", "Batal Aja"):
            with self.subTest(message=message):
                result = self._run_pending(
                    "name",
                    message,
                    {},
                )
                self.assertEqual(
                    self.memory.get_session("pending-name")["name"],
                    message,
                )
                self.assertEqual(result["field"], "people")

    def test_date_answer_wins_in_date_context(self):
        result = self._run_pending(
            "date",
            "Jumat depan",
            {"name": "Rizal", "people": 3},
        )
        self.assertEqual(
            self.memory.get_session("pending-date")["date"],
            "2026-07-31",
        )
        self.assertEqual(result["field"], "time")

    def test_time_answer_wins_in_time_context(self):
        result = self._run_pending(
            "time",
            "setengah delapan malam",
            {"name": "Rizal", "people": 3, "date": "2026-07-31"},
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(
            self.memory.get_session("pending-time")["time"],
            "19:30",
        )

    def test_ambiguous_pending_values_get_focused_clarification(self):
        date_result = self._run_pending(
            "date",
            "tanggal 5",
            {"name": "Rizal", "people": 3},
        )
        time_result = self._run_pending(
            "time",
            "jam 7",
            {"name": "Rizal", "people": 3, "date": "2026-07-31"},
        )
        self.assertIn("bulan dan tahun", date_result["response"])
        self.assertIn("07.00 atau 19.00", time_result["response"])

    def test_confirmation_and_rejection_synonyms_are_contextual(self):
        for message in (
            "ya",
            "iya",
            "iya benar",
            "ya lanjut",
            "iya lanjut",
            "oke",
            "oke lanjut",
            "oke gas",
            "sip",
            "sip lanjut",
            "betul",
            "betul lanjutkan",
            "benar",
            "sudah benar",
            "lanjut",
            "lanjutkan",
            "gas",
            "silakan lanjutkan",
            "boleh lanjut",
            "setuju",
            "sesuai",
            "pas",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    self.agent._detect_confirmation_intent(message),
                    (CONFIRM, None),
                )
        for message in (
            "tidak",
            "tidak usah",
            "tidak jadi",
            "nggak",
            "nggak jadi",
            "gak jadi",
            "jangan",
            "jangan lanjut",
            "batal",
            "batal aja",
            "batalkan saja",
            "sudah tidak perlu",
            "jangan diproses",
            "tidak jadi pesan",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    self.agent._detect_confirmation_intent(message),
                    (REJECT, None),
                )

    def test_negative_phrase_precedes_embedded_positive_word(self):
        for message in (
            "nggak jadi lanjut",
            "iya tapi jangan lanjut",
            "oke tapi batal aja",
            "ubah jam tapi jangan lanjut",
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_confirmation(message), "reject")

    def test_create_rejection_spellings_are_normalized_as_bounded_phrases(self):
        for message in (
            "batalkan reservasinya",
            "batalin reservasinya",
            "batalin reservasi",
            "batalkan reservasi",
            "ga jadi",
            "nga jadi",
            "ngga jadi",
            "nggak jadi",
            "gak jadi",
            "enggak jadi",
            "jangan lanjut",
            "ga usah",
            "tidak usah",
            "tidak jadi",
            "batal aja",
            "batalkan saja",
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_confirmation(message), "reject")

    def test_natural_field_correction_is_detected(self):
        cases = {
            "jamnya ganti": "time",
            "tanggalnya pindah": "date",
            "orangnya ditambah": "people",
            "orangnya dikurangi": "people",
            "namanya mau diganti": "name",
            "ganti hari": "date",
            "jadinya tiga orang": "people",
            "jamnya jadi delapan malam": "time",
        }
        for message, field in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    self.agent._detect_confirmation_intent(message),
                    (EDIT_FIELD, field),
                )


class OneShotExtractionTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryManager()
        self.agent = ReservationAgent(
            memory_manager=self.memory,
            clock=lambda: FROZEN_NOW,
        )

    def _run(self, session_id, message):
        state = {
            "intent": "reservation",
            "name": None,
            "people": None,
            "date": None,
            "time": None,
            "completed": False,
            "awaiting_confirmation": False,
        }
        return asyncio.run(
            self.agent.run(
                [{"action": "collect_missing_fields"}],
                state,
                message,
                session_id=session_id,
            )
        )

    def test_complete_one_shot_reaches_confirmation(self):
        result = self._run(
            "one-shot",
            "Booking buat 3 orang tanggal 30 Juli 2026 jam 7 malam atas nama Rizal",
        )
        state = self.memory.get_session("one-shot")
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(
            {key: state[key] for key in ("name", "people", "date", "time")},
            {
                "name": "Rizal",
                "people": 3,
                "date": "2026-07-30",
                "time": "19:00",
            },
        )

    def test_partial_one_shot_asks_only_for_missing_field(self):
        result = self._run(
            "partial-shot",
            "Booking buat 3 orang tanggal 30 Juli 2026 atas nama Rizal",
        )
        self.assertEqual(result["field"], "time")
        self.assertEqual(result["response"], "Jam berapa?")
        self.assertEqual(
            self.memory.get_session("partial-shot")["asked_fields"],
            ["name", "people", "date", "time"],
        )

    def test_natural_one_shots_extract_known_fields_and_clarify_only_missing(self):
        cases = (
            (
                "natural-shot-time",
                "mau booking besok malam berdua atas nama Rizal",
                "time",
                {
                    "name": "Rizal",
                    "people": 2,
                    "date": "2026-07-27",
                },
            ),
            (
                "natural-shot-name",
                "pesan meja Jumat jam tujuh malam untuk empat orang",
                "name",
                {
                    "people": 4,
                    "date": "2026-07-31",
                    "time": "19:00",
                },
            ),
            (
                "natural-shot-people",
                "tolong reservasi atas nama Andi untuk lusa malam",
                "people",
                {
                    "name": "Andi",
                    "date": "2026-07-28",
                },
            ),
            (
                "natural-shot-family",
                "booking meja buat kami berlima besok jam delapan malam",
                "name",
                {
                    "people": 5,
                    "date": "2026-07-27",
                    "time": "20:00",
                },
            ),
        )
        for session_id, message, missing_field, expected in cases:
            with self.subTest(message=message):
                result = self._run(session_id, message)
                state = self.memory.get_session(session_id)
                self.assertEqual(result["field"], missing_field)
                for field, value in expected.items():
                    self.assertEqual(state[field], value)

    def test_invalid_extracted_field_is_not_accepted(self):
        extractor = ReservationEntityExtractor(clock=lambda: FROZEN_NOW)
        result = asyncio.run(
            extractor.extract(
                "Booking buat 99 orang tanggal 30 Februari 2026 jam 25:00 atas nama Rizal"
            )
        )
        self.assertEqual(result, {"name": "Rizal"})


class UpdateCancelLanguageTests(unittest.TestCase):
    def test_update_field_and_value_language(self):
        agent = UpdateReservationAgent(clock=lambda: FROZEN_NOW)
        self.assertEqual(agent._resolve_field("jamnya jadi 8 malam"), "time")
        self.assertEqual(agent._parse_new_value("time", "jamnya jadi 8 malam"), "20:00")
        self.assertEqual(agent._parse_new_value("people", "orangnya jadi tiga"), 3)

    def test_cancel_word_rejects_cancellation_confirmation(self):
        self.assertEqual(parse_confirmation("batal"), "reject")
        self.assertIsInstance(CancelReservationAgent(), CancelReservationAgent)

    def test_target_field_allowlist(self):
        for message, expected in (
            ("ubah tanggal", "date"),
            ("ganti jam", "time"),
            ("orangnya jadi tiga", "people"),
            ("namanya ganti Rizal", "name"),
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_target_field(message), expected)
        self.assertIsNone(parse_target_field("ubah status"))

    def test_additional_natural_intent_variants(self):
        cases = {
            "jadwalnya mau saya ganti": "update_reservation",
            "orangnya mau ditambah": "update_reservation",
            "ubah waktu booking saya": "update_reservation",
            "saya jadi nggak datang": "cancel_reservation",
            "bookingnya batal aja": "cancel_reservation",
            "tolong hapus pesanan meja saya": "cancel_reservation",
            "booking saya masih ada?": "view_reservation",
            "reservasi saya sudah tercatat belum?": "view_reservation",
            "coba lihat pesanan saya": "view_reservation",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    IntentClassifier.detect_reservation_intent(message),
                    expected,
                )

    def test_additional_handoff_variants_remain_explicit(self):
        for message in (
            "saya mau ngomong sama admin",
            "bisa sambungkan ke petugas?",
            "saya perlu bantuan manusia",
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    HandoffDetector.is_explicit_human_request(message)
                )

    def test_vague_help_does_not_trigger_mutation_or_handoff(self):
        message = "Saya perlu bantuan"
        self.assertIsNone(IntentClassifier.detect_reservation_intent(message))
        self.assertFalse(HandoffDetector.is_explicit_human_request(message))


class StructuredFallbackTests(unittest.TestCase):
    FALLBACK = {"intent": "general", "confidence": 0.0}

    @staticmethod
    def _classifier(response):
        provider = SimpleNamespace(chat=AsyncMock(return_value=response))
        return IntentClassifier(provider=provider)

    def test_valid_flat_structured_fallback_is_validated(self):
        payload = {
            "intent": "reservation",
            "name": "Rizal",
            "people": 3,
            "date": "2026-07-30",
            "time": "19:00",
            "target_field": "time",
            "confirmation": "confirm",
            "confidence": 0.91,
            "ambiguity_reason": "deterministic rules did not match",
        }
        classifier = self._classifier(json.dumps(payload))
        self.assertEqual(
            asyncio.run(classifier.classify("tolong bantu saya")),
            payload,
        )

    def test_unknown_key_and_unknown_enum_fail_closed(self):
        cases = (
            '{"intent":"update_reservation","confidence":0.9,"reservation_id":2}',
            '{"intent":"update_reservation","confidence":0.9,"reservation_reference":"RSV_unsafe"}',
            '{"intent":"reservation","confidence":0.9,"sql":"DROP"}',
            '{"intent":"unknown","confidence":0.9}',
            '{"intent":"reservation","confidence":0.9,"target_field":"status"}',
            '{"intent":"reservation","confidence":0.9,"confirmation":"maybe"}',
        )
        for response in cases:
            with self.subTest(response=response):
                classifier = self._classifier(response)
                self.assertEqual(
                    asyncio.run(classifier.classify("tolong bantu saya")),
                    self.FALLBACK,
                )

    def test_invalid_extracted_values_fail_closed(self):
        for response in (
            '{"intent":"reservation","confidence":0.9,"people":99}',
            '{"intent":"reservation","confidence":0.9,"date":"30 Juli"}',
            '{"intent":"reservation","confidence":0.9,"time":"jam 7"}',
            '{"intent":"reservation","confidence":0.9,"name":"Bad/Name"}',
        ):
            with self.subTest(response=response):
                classifier = self._classifier(response)
                self.assertEqual(
                    asyncio.run(classifier.classify("tolong bantu saya")),
                    self.FALLBACK,
                )

    def test_provider_prose_is_rejected(self):
        classifier = self._classifier(
            'Baik: {"intent":"reservation","confidence":0.9}'
        )
        self.assertEqual(
            asyncio.run(classifier.classify("tolong bantu saya")),
            self.FALLBACK,
        )

    def test_deterministic_field_wins_over_ai_conflict(self):
        classifier = self._classifier(
            '{"intent":"reservation","confidence":0.9,"people":4}'
        )
        result = asyncio.run(
            classifier.classify("Tolong bantu untuk 3 orang")
        )
        self.assertEqual(result["intent"], "reservation")
        self.assertEqual(result["people"], 3)


class GreetingRegressionTests(unittest.TestCase):
    def test_customer_greeting_contains_no_placeholder_or_internal_details(self):
        response = asyncio.run(
            GreetingAgent().run(
                [{"action": "respond"}],
                {},
                "Halo",
            )
        )["response"]
        self.assertNotIn("placeholder", response.casefold())
        self.assertNotIn("intent", response.casefold())
        self.assertEqual(response, "Halo! Saya AURA. Ada yang bisa saya bantu?")


if __name__ == "__main__":
    unittest.main()
