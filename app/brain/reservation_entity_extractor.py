import re
from typing import Any

from app.core.input_validation import (
    InputValidationError,
    normalize_reservation_name,
    validate_reservation_people,
)

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


def normalize_natural_reservation_name(value: str) -> str:
    """Extract a strict stored name from a natural-language name candidate."""
    candidate = _NAME_FIELD_TRANSITION.split(value, maxsplit=1)[0]
    candidate = _TRAILING_SENTENCE_DELIMITER.sub("", candidate, count=1)
    return normalize_reservation_name(candidate)


class ReservationEntityExtractor:
    """Extract reservation entities from a single user message."""

    async def extract(self, message: str) -> dict[str, Any]:
        text = message.lower()
        result: dict[str, Any] = {}

        name_match = re.search(r"\batas nama\s+(.+)$", message, re.IGNORECASE)
        if name_match:
            try:
                result["name"] = normalize_natural_reservation_name(
                    name_match.group(1)
                )
            except InputValidationError:
                pass

        people_matches = re.findall(r"(?<![0-9.])([0-9]+)(?![0-9.])\s+orang\b", text)
        if len(people_matches) == 1:
            try:
                result["people"] = validate_reservation_people(
                    int(people_matches[0])
                )
            except InputValidationError:
                pass

        if "besok" in text:
            result["date"] = "besok"
        elif "hari ini" in text:
            result["date"] = "hari ini"
        elif "lusa" in text:
            result["date"] = "lusa"

        time_match = re.search(r"jam\s+(\d{1,2})", text)
        if time_match:
            hour = int(time_match.group(1))
            if "malam" in text and hour < 12:
                hour += 12
            elif "sore" in text and hour < 12:
                hour += 12
            elif "siang" in text and hour < 7:
                hour += 12
            elif hour < 7 and ("pagi" in text or "pukul" in text):
                hour += 12
            elif hour <= 7:
                hour += 12
            result["time"] = f"{hour:02d}:00"

        return result
