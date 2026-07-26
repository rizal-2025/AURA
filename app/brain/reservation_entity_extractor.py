import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.brain.indonesian_nlu import (
    NUMBER_WORDS,
    normalize_indonesian_text,
    parse_people_count,
)
from app.core.input_validation import (
    InputValidationError,
    normalize_reservation_name,
)
from app.utils.datetime_parser import DatetimeParser

_NAME_FIELD_TRANSITION = re.compile(
    r"\s*(?:,\s*|\s+)(?="
    r"(?:"
    r"untuk|tanggal|pada\s+tanggal|"
    r"hari\s+(?:ini|senin|selasa|rabu|kamis|jumat|sabtu|minggu)|"
    r"besok|lusa|jam|pukul|waktu|jumlah\s+orang"
    r")\b"
    r")",
    re.IGNORECASE,
)
_TRAILING_SENTENCE_DELIMITER = re.compile(r"\s*[,!?;:]\s*$")
_ORDINAL_RESERVATION_IDS = {
    "pertama": 1,
    "kedua": 2,
    "ketiga": 3,
    "keempat": 4,
    "kelima": 5,
}


def normalize_natural_reservation_name(value: str) -> str:
    """Extract a strict stored name from a natural-language name candidate."""
    candidate = _NAME_FIELD_TRANSITION.split(value, maxsplit=1)[0]
    candidate = _TRAILING_SENTENCE_DELIMITER.sub("", candidate, count=1)
    return normalize_reservation_name(candidate)


def parse_reservation_id(value: str) -> int | None:
    """Parse one bounded positive ID while an agent is asking for selection."""
    normalized = normalize_indonesian_text(value)
    if not normalized:
        return None

    number_words = "|".join(
        sorted(
            (
                re.escape(word)
                for word in set(NUMBER_WORDS) | set(_ORDINAL_RESERVATION_IDS)
            ),
            key=len,
            reverse=True,
        )
    )
    candidate_pattern = rf"(?P<value>[0-9]+|{number_words})"
    patterns = (
        rf"{candidate_pattern}",
        rf"(?:yang\s+)?(?:reservasi\s+)?(?:id|nomor)\s+{candidate_pattern}",
        rf"(?:reservasi|pesanan saya)\s+yang\s+(?:nomor\s+)?{candidate_pattern}",
    )
    matched_value = None
    for pattern in patterns:
        match = re.fullmatch(pattern, normalized)
        if match:
            matched_value = match.group("value")
            break

    if matched_value is None:
        return None
    if matched_value.isdigit():
        candidate = int(matched_value)
    else:
        candidate = _ORDINAL_RESERVATION_IDS.get(
            matched_value,
            NUMBER_WORDS.get(matched_value),
        )
    if candidate is None or candidate < 1 or candidate > (2**63) - 1:
        return None
    return candidate


class ReservationEntityExtractor:
    """Extract reservation entities from a single user message."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self.clock = clock

    async def extract(self, message: str) -> dict[str, Any]:
        result: dict[str, Any] = {}

        name_match = re.search(r"\batas nama\s+(.+)$", message, re.IGNORECASE)
        if name_match:
            try:
                result["name"] = normalize_natural_reservation_name(
                    name_match.group(1)
                )
            except InputValidationError:
                pass

        people = parse_people_count(message, allow_bare=False)
        if people is not None:
            result["people"] = people

        parsed_date = DatetimeParser.parse_date(message, clock=self.clock)
        if parsed_date is not None:
            result["date"] = parsed_date

        parsed_time = DatetimeParser.parse_time(message)
        if parsed_time is not None:
            result["time"] = parsed_time

        return result
