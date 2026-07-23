import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.brain.classifier import IntentClassifier


class TestBrainIntentClassifier(unittest.TestCase):
    def test_classify_returns_intent_and_confidence(self):
        classifier = IntentClassifier()
        classifier.ai = type("DummyAI", (), {"chat": AsyncMock(return_value='{"intent": "reservation", "confidence": 0.95}')})()

        result = asyncio.run(classifier.classify("Bantu saya"))

        self.assertEqual(result["intent"], "reservation")
        self.assertEqual(result["confidence"], 0.95)

    def test_normalizes_safe_indonesian_affixes(self):
        self.assertEqual(
            IntentClassifier.normalize_reservation_text(
                "  Saya Mau Mengubah Reservasi  ",
            ),
            "saya mau ubah reservasi",
        )
        self.assertEqual(
            IntentClassifier.normalize_reservation_text("Membatalkan booking"),
            "batal booking",
        )
        self.assertEqual(
            IntentClassifier.normalize_reservation_text("Melihat pemesanan"),
            "lihat pemesanan",
        )

    def test_classifies_natural_reservation_action_variations_without_ai(self):
        ai = type("DummyAI", (), {"chat": AsyncMock(return_value="{}")})()
        classifier = IntentClassifier(provider=ai)
        cases = {
            "ubah reservasi saya": "update_reservation",
            "saya ingin ubah reservasi": "update_reservation",
            "saya mau mengubah reservasi": "update_reservation",
            "reservasi saya ingin diubah": "update_reservation",
            "tolong edit booking saya": "update_reservation",
            "saya ingin ganti jadwal reservasi": "update_reservation",
            "ganti jumlah orang untuk reservasi saya": "update_reservation",
            "bisa bantu revisi reservasi saya": "update_reservation",
            "tolong ganti jumlah orang": "update_reservation",
            "batalkan reservasi saya": "cancel_reservation",
            "saya ingin batal reservasi": "cancel_reservation",
            "reservasi saya mau dibatalkan": "cancel_reservation",
            "cancel booking saya": "cancel_reservation",
            "tolong batalkan pemesanan saya": "cancel_reservation",
            "lihat reservasi saya": "view_reservation",
            "saya ingin melihat reservasi": "view_reservation",
            "tampilkan booking saya": "view_reservation",
            "reservasi saya apa saja": "view_reservation",
            "cek pesanan meja saya": "view_reservation",
            "saya ingin reservasi meja": "reservation",
            "buatkan reservasi": "reservation",
            "saya mau booking meja": "reservation",
            "pesan meja untuk besok": "reservation",
        }

        for message, expected_intent in cases.items():
            with self.subTest(message=message):
                result = asyncio.run(classifier.classify(message))
                self.assertEqual(result["intent"], expected_intent)
                self.assertEqual(result["confidence"], 0.99)

        ai.chat.assert_not_awaited()

    def test_classifies_complete_safe_greetings_without_ai(self):
        ai = type("FailingAI", (), {"chat": AsyncMock(side_effect=AssertionError("AI must not run"))})()
        classifier = IntentClassifier(provider=ai)
        for message in (
            "Halo",
            "  Hai!  ",
            "HI...",
            "hello",
            "Selamat pagi",
            "SELAMAT SIANG!",
            "Selamat sore",
            "Selamat malam.",
        ):
            with self.subTest(message=message):
                result = asyncio.run(classifier.classify(message))
                self.assertEqual(result, {"intent": "greeting", "confidence": 0.99})
        ai.chat.assert_not_awaited()

    def test_mixed_greeting_messages_are_not_reduced_to_greeting(self):
        ai = type("DummyAI", (), {"chat": AsyncMock(return_value='{"intent":"general","confidence":0.9}')})()
        classifier = IntentClassifier(provider=ai)
        cases = {
            "Halo, saya mau reservasi": "reservation",
            "Hai, batalkan reservasi saya": "cancel_reservation",
            "Selamat pagi, saya ingin bicara dengan petugas": "general",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                result = asyncio.run(classifier.classify(message))
                self.assertEqual(result["intent"], expected)
        self.assertEqual(ai.chat.await_count, 1)

    def test_negated_informational_and_mixed_actions_remain_general(self):
        classifier = IntentClassifier(
            provider=type("DummyAI", (), {"chat": AsyncMock(return_value="{}")})(),
        )
        cases = (
            "saya tidak ingin mengubah reservasi",
            "jangan batalkan reservasi saya",
            "saya belum mau booking",
            "bagaimana cara mengubah reservasi?",
            "apa itu pembatalan reservasi?",
            "tolong ubah atau batalkan reservasi saya",
        )

        for message in cases:
            with self.subTest(message=message):
                result = asyncio.run(classifier.classify(message))
                self.assertEqual(result["intent"], "general")
                self.assertEqual(result["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
