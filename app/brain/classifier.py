import json
import re
from typing import Any

from app.services.ai.factory import get_ai_provider


class IntentClassifier:
    """Classify user message into an intent with optional confidence score."""

    DEFAULT_INTENT = "general"
    DEFAULT_CONFIDENCE = 0.0

    _NORMALIZATION_RULES = (
        (r"\bmengubah\b", "ubah"),
        (r"\bdiubah\b", "ubah"),
        (r"\bmengganti\b", "ganti"),
        (r"\bdiganti\b", "ganti"),
        (r"\bbatalkan\b", "batal"),
        (r"\bmembatalkan\b", "batal"),
        (r"\bdibatalkan\b", "batal"),
        (r"\bmelihat\b", "lihat"),
    )
    _RESERVATION_WORDS = {"reservasi", "booking", "pemesanan"}
    _UPDATE_WORDS = {"ubah", "ganti", "edit", "revisi", "update"}
    _RESERVATION_FIELD_WORDS = {"nama", "jumlah", "orang", "tanggal", "jam", "waktu"}
    _CANCEL_WORDS = {"batal", "cancel"}
    _VIEW_WORDS = {"lihat", "tampilkan", "tampil", "cek", "daftar"}
    _NEGATION_WORDS = {"tidak", "jangan", "belum"}
    _INFORMATIONAL_PREFIXES = (
        "bagaimana",
        "apa itu",
        "jelaskan",
        "cara ",
    )

    def __init__(self, provider: Any | None = None):
        self.ai = provider or get_ai_provider()

    async def classify(self, message: str) -> dict[str, Any]:
        rule_based_intent = self.detect_reservation_intent(message)
        if rule_based_intent is not None:
            return {
                "intent": rule_based_intent,
                "confidence": 0.99 if rule_based_intent != "general" else 0.0,
            }

        prompt = self._build_prompt(message)
        response = await self.ai.chat(prompt)

        try:
            data = json.loads(response)
            intent = data.get("intent", self.DEFAULT_INTENT)
            confidence = data.get("confidence", self.DEFAULT_CONFIDENCE)
            return {
                "intent": intent,
                "confidence": confidence,
            }
        except Exception:
            return {
                "intent": self.DEFAULT_INTENT,
                "confidence": self.DEFAULT_CONFIDENCE,
            }

    @classmethod
    def normalize_reservation_text(cls, message: str) -> str:
        """Normalize only the Indonesian forms needed for reservation routing."""
        normalized = " ".join(message.lower().strip().split())
        for pattern, replacement in cls._NORMALIZATION_RULES:
            normalized = re.sub(pattern, replacement, normalized)
        return normalized

    @classmethod
    def detect_reservation_intent(cls, message: str) -> str | None:
        """Return a safe, deterministic reservation action intent when explicit.

        The matcher requires both an action and a reservation object (except the
        common creation phrase ``pesan meja``). Negated, informational, and
        mixed-action input remains general instead of starting a workflow.
        """
        normalized = cls.normalize_reservation_text(message)
        tokens = set(re.findall(r"[a-z]+", normalized))

        if not normalized:
            return None

        if (
            tokens.intersection(cls._NEGATION_WORDS)
            or normalized.startswith(cls._INFORMATIONAL_PREFIXES)
        ):
            return "general"

        # Preserve the concise English and Indonesian commands already exposed
        # by the API while using the flexible rules below for natural wording.
        legacy_commands = {
            "reservasi saya": "view_reservation",
            "show my reservation": "view_reservation",
            "update my reservation": "update_reservation",
            "cancel my reservation": "cancel_reservation",
        }
        if normalized in legacy_commands:
            return legacy_commands[normalized]

        has_reservation_object = bool(tokens.intersection(cls._RESERVATION_WORDS)) or (
            "pesanan meja" in normalized
        )
        has_update = bool(tokens.intersection(cls._UPDATE_WORDS)) and (
            has_reservation_object
            or bool(tokens.intersection(cls._RESERVATION_FIELD_WORDS))
        )
        has_cancel = has_reservation_object and bool(
            tokens.intersection(cls._CANCEL_WORDS)
        )
        has_view = has_reservation_object and (
            bool(tokens.intersection(cls._VIEW_WORDS))
            or "apa saja" in normalized
        )

        # Creation needs an explicit creation cue. A bare mention of a
        # reservation must not reset a conversation into the create flow.
        has_create = False
        if not (has_update or has_cancel or has_view):
            has_create = (
                (has_reservation_object and bool(tokens.intersection({"buat", "buatkan", "pesan"})))
                or (
                    has_reservation_object
                    and bool(tokens.intersection({"ingin", "mau"}))
                    and bool(tokens.intersection({"reservasi", "booking"}))
                )
                or "pesan meja" in normalized
            )

        matched_intents = [
            intent
            for intent, matched in (
                ("update_reservation", has_update),
                ("cancel_reservation", has_cancel),
                ("view_reservation", has_view),
                ("reservation", has_create),
            )
            if matched
        ]
        if len(matched_intents) == 1:
            return matched_intents[0]
        if len(matched_intents) > 1:
            return "general"
        return None

    def _build_prompt(self, message: str) -> str:
        intents = self._get_supported_intents()
        return f"""
Kamu adalah Intent Classifier untuk AURA.

Tugasmu adalah memilih satu intent dari daftar berikut:
{intents}

Aturan:
1. Jawab hanya dalam format JSON.
2. Format harus:
{{"intent": "reservation", "confidence": 0.95}}
3. Jika tidak yakin, gunakan intent general.
4. Jangan tambahkan teks lain.

Pesan user:
{message}
"""

    def _get_supported_intents(self) -> list[str]:
        return [
            "reservation",
            "view_reservation",
            "update_reservation",
            "cancel_reservation",
            "menu",
            "promo",
            "faq",
            "complaint",
            "general",
        ]
