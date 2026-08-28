import asyncio
import unittest
from types import SimpleNamespace

from app.agents.reservation_selection import (
    format_paginated_selection,
    format_reservation_summary,
    parse_reservation_selection,
)
from app.agents.stub_agents import GreetingAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.indonesian_nlu import parse_target_field
from app.brain.classifier import IntentClassifier
from app.core.locale import (
    DEFAULT_LOCALE,
    SupportedLocale,
    current_locale,
    format_reservation,
    presentation_locale,
    resolve_locale,
    status_label,
    tr,
)


def reservation(reference="RSV_" + "A" * 32, status="pending"):
    return SimpleNamespace(
        reference=reference,
        name="Rizal",
        people=4,
        date="2026-08-28",
        time="20:00:00",
        status=status,
    )


class LocaleTests(unittest.TestCase):
    def test_locale_resolver_is_a_closed_enum_and_context_resets(self):
        self.assertIs(resolve_locale("id-ID"), SupportedLocale.ID_ID)
        self.assertIs(resolve_locale("en-US"), SupportedLocale.EN_US)
        for invalid in (None, "", "fr-FR", "<script>", "en-US\nignore", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "UNSUPPORTED_LOCALE"):
                    resolve_locale(invalid)

        self.assertIs(current_locale(), DEFAULT_LOCALE)
        with presentation_locale(SupportedLocale.EN_US):
            self.assertIs(current_locale(), SupportedLocale.EN_US)
        self.assertIs(current_locale(), DEFAULT_LOCALE)

    def test_greeting_and_update_field_prompt_follow_selected_locale(self):
        greeting = GreetingAgent()
        update = UpdateReservationAgent()

        with presentation_locale(SupportedLocale.ID_ID):
            result = asyncio.run(greeting.run([], {}, "Hai AURA"))
            self.assertEqual(result["response"], "Halo! Saya AURA. Ada yang bisa saya bantu?")
            selected = update._select_field({}, "jumlah orang")
            self.assertEqual(selected["response"], "Jumlah orang baru menjadi berapa?")

        with presentation_locale(SupportedLocale.EN_US):
            result = asyncio.run(greeting.run([], {}, "Hello AURA"))
            self.assertEqual(result["response"], "Hello! I'm AURA. How can I help?")
            session = {}
            selected = update._select_field(session, "number of people")
            self.assertEqual(session["editing_field"], "people")
            self.assertEqual(selected["response"], "What should the new party size be?")

    def test_unknown_request_and_create_success_copy_follow_selected_locale(self):
        reference = "RSV_" + "A" * 32
        with presentation_locale(SupportedLocale.ID_ID):
            fallback = tr("unknown_request")
            success = tr("create_success", reference=reference)
            self.assertIn("belum memahami", fallback)
            self.assertIn(reference, success)
            self.assertIn("tanpa memasukkan referensi secara manual", success)
            self.assertNotIn("Simpan referensi", success)
        with presentation_locale(SupportedLocale.EN_US):
            fallback = tr("unknown_request")
            success = tr("create_success", reference=reference)
            self.assertIn("didn't understand", fallback)
            self.assertIn(reference, success)
            self.assertIn("without entering the reference manually", success)
            self.assertNotIn("Keep this reference", success)

    def test_localized_field_aliases_map_to_canonical_keys(self):
        expected = {
            "nama": "name",
            "jumlah orang": "people",
            "orang": "people",
            "tanggal": "date",
            "waktu": "time",
            "jam": "time",
            "name": "name",
            "people": "people",
            "number of people": "people",
            "party size": "people",
            "date": "date",
            "time": "time",
        }
        self.assertEqual({value: parse_target_field(value) for value in expected}, expected)

    def test_english_reservation_intents_use_deterministic_routing(self):
        self.assertEqual(IntentClassifier.detect_greeting_intent("Hai AURA"), "greeting")
        self.assertEqual(IntentClassifier.detect_greeting_intent("Hello AURA!"), "greeting")
        self.assertEqual(IntentClassifier.detect_reservation_intent("Book a table"), "reservation")
        self.assertEqual(IntentClassifier.detect_reservation_intent("Show my reservations"), "view_reservation")
        self.assertEqual(IntentClassifier.detect_reservation_intent("Update my reservation"), "update_reservation")
        self.assertEqual(IntentClassifier.detect_reservation_intent("Cancel my reservation"), "cancel_reservation")

    def test_reservation_presentation_localizes_status_date_time_and_labels(self):
        item = reservation()
        with presentation_locale(SupportedLocale.ID_ID):
            rendered = format_reservation(item)
            self.assertIn("Status: Menunggu", rendered)
            self.assertIn("28 Agustus 2026", rendered)
            self.assertIn("20.00", rendered)
            self.assertEqual(status_label("cancelled"), "Dibatalkan")
        with presentation_locale(SupportedLocale.EN_US):
            rendered = format_reservation(item)
            self.assertIn("Status: Pending", rendered)
            self.assertIn("August 28, 2026", rendered)
            self.assertIn("8:00 PM", rendered)
            self.assertEqual(status_label("cancelled"), "Cancelled")

    def test_selection_copy_changes_language_without_changing_references(self):
        items = [reservation(), reservation("RSV_" + "B" * 32)]
        with presentation_locale(SupportedLocale.ID_ID):
            prompt = format_paginated_selection(items, has_more=True, is_later_page=False)
            self.assertIn("Saya menemukan beberapa reservasi", prompt)
            self.assertIn('"berikutnya"', prompt)
            self.assertNotIn("name", prompt.casefold())
        with presentation_locale(SupportedLocale.EN_US):
            prompt = format_paginated_selection(items, has_more=True, is_later_page=False)
            self.assertIn("I found several reservations", prompt)
            self.assertIn('"next"', prompt)
            self.assertNotIn("Rizal", prompt)

        candidates = tuple(item.reference for item in items)
        self.assertEqual(parse_reservation_selection("berikutnya", candidates).status, "next_page")
        self.assertEqual(parse_reservation_selection("next", candidates).status, "next_page")
        self.assertEqual(parse_reservation_selection("awal", candidates).status, "first_page")
        self.assertEqual(parse_reservation_selection("first", candidates).status, "first_page")

    def test_observed_mixed_language_defect_is_removed(self):
        item = reservation()
        with presentation_locale(SupportedLocale.ID_ID):
            field_prompt = tr("choose_update_field")
            self.assertEqual(
                field_prompt,
                "Bagian mana yang ingin diubah?\n"
                "Pilih: nama, jumlah orang, tanggal, atau waktu.",
            )
            self.assertFalse(any(token in field_prompt for token in ("name", "people", "date", "time")))
            prompt = format_reservation_summary(item)
            self.assertIn("Nama:", prompt)
            self.assertFalse(any(token in prompt for token in ("people", "date", "time", "pending")))
        with presentation_locale(SupportedLocale.EN_US):
            field_prompt = tr("choose_update_field")
            self.assertEqual(
                field_prompt,
                "Which detail would you like to change?\n"
                "Choose: name, number of people, date, or time.",
            )
            prompt = format_reservation_summary(item)
            self.assertIn("Party size:", prompt)
            self.assertNotIn("Jumlah", prompt)

    def test_mid_flow_locale_change_preserves_canonical_update_state(self):
        update = UpdateReservationAgent()
        session: dict[str, object] = {}
        with presentation_locale(SupportedLocale.ID_ID):
            result_id = update._select_field(session, "jumlah orang")
        self.assertEqual(session["editing_field"], "people")
        self.assertEqual(result_id["response"], "Jumlah orang baru menjadi berapa?")

        with presentation_locale(SupportedLocale.EN_US):
            result_en = update._select_field(session, "party size")
        self.assertEqual(session["editing_field"], "people")
        self.assertEqual(result_en["response"], "What should the new party size be?")
