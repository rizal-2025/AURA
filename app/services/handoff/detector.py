import re

from app.brain.classifier import IntentClassifier
from app.brain.indonesian_nlu import (
    contains_bounded_phrase,
    normalize_indonesian_text,
)


class HandoffDetector:
    """Detect safe, explicit reasons to pause automated assistance."""

    EXPLICIT_HUMAN_PATTERNS = (
        r"\bhubungkan\b.*\b(admin|manusia|orang|petugas|staf|operator|owner|customer service|rizal)\b",
        r"\bbicara\b.*\b(admin|manusia|orang|petugas|staf|operator|owner|customer service|rizal)\b",
        r"\bngomong\b.*\b(admin|manusia|orang|petugas|staf|operator|owner|customer service)\b",
        r"\bpanggil\b.*\b(admin|petugas|staf|operator)\b",
        r"\b(?:butuh|perlu) bantuan (?:dari )?(admin|manusia|orang|petugas|staf|operator)\b",
        r"\bdilayani langsung\b",
        r"\bchat\b.*\bcustomer service\b",
    )
    FRUSTRATION_PHRASES = frozenset(
        {
        "ini tidak membantu",
        "kok gagal terus",
        "saya sudah coba berkali kali",
        "pelayanan ini buruk",
        "saya kesal",
        "botnya tidak mengerti",
        "ribet banget",
        "dari tadi tidak bisa",
        }
    )
    INFORMATIONAL_PREFIXES = (
        "bagaimana",
        "apa itu",
        "jelaskan",
        "cara ",
    )
    KNOWN_NON_ACTION_PHRASES = (
        "halo",
        "hai",
        "terima kasih",
        "makasih",
        "selamat pagi",
        "selamat siang",
        "selamat malam",
        "tolong dong",
        "saya bingung",
        "bagaimana ya",
        "bisa bantu",
    )

    @classmethod
    def normalize(cls, message: str) -> str:
        return normalize_indonesian_text(message)

    @classmethod
    def is_explicit_human_request(cls, message: str) -> bool:
        normalized = cls.normalize(message)
        return any(re.search(pattern, normalized) for pattern in cls.EXPLICIT_HUMAN_PATTERNS)

    @classmethod
    def is_frustrated(cls, message: str) -> bool:
        normalized = cls.normalize(message)
        return contains_bounded_phrase(normalized, cls.FRUSTRATION_PHRASES)

    @classmethod
    def is_ambiguous_reservation_action(cls, message: str) -> bool:
        """True only when update and cancel are both explicit candidates."""
        normalized = IntentClassifier.normalize_reservation_text(message)
        tokens = set(re.findall(r"[a-z]+", normalized))
        if tokens.intersection(IntentClassifier._NEGATION_WORDS):
            return False
        has_object = bool(tokens.intersection(IntentClassifier._RESERVATION_WORDS)) or (
            "pesanan meja" in normalized
        )
        return (
            has_object
            and bool(tokens.intersection(IntentClassifier._UPDATE_WORDS))
            and bool(tokens.intersection(IntentClassifier._CANCEL_WORDS))
        )

    @classmethod
    def is_safe_non_action_message(cls, message: str) -> bool:
        """Negated and informational requests are valid, non-destructive input."""
        normalized = cls.normalize(message)
        return (
            normalized in cls.KNOWN_NON_ACTION_PHRASES
            or IntentClassifier.detect_reservation_intent(message) == "general"
        )

    @classmethod
    def is_deterministically_misunderstood(cls, message: str) -> bool:
        """Identify unknown token-like messages without sending them to AI first."""
        normalized = cls.normalize(message)
        if not normalized:
            return True
        if normalized.startswith(cls.INFORMATIONAL_PREFIXES):
            return False
        if normalized in cls.KNOWN_NON_ACTION_PHRASES:
            return False

        detected_intent = IntentClassifier.detect_reservation_intent(message)
        if detected_intent not in {None, "general"}:
            return False

        tokens = set(normalized.split())
        known_reservation_tokens = (
            IntentClassifier._RESERVATION_WORDS
            | IntentClassifier._UPDATE_WORDS
            | IntentClassifier._CANCEL_WORDS
            | IntentClassifier._VIEW_WORDS
            | {"buat", "buatkan", "pesan", "meja"}
        )
        # A meaningful negated reservation request is valid non-action input;
        # random text such as "zxqv qwerty tidak jelas" has no such context.
        if tokens.intersection(known_reservation_tokens):
            return False
        return True

    @staticmethod
    def is_low_confidence(intent: str, confidence) -> bool:
        return intent in {"general", "ambiguous"} and isinstance(confidence, (int, float)) and confidence < 0.5
