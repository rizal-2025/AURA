import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from app.brain.indonesian_nlu import parse_people_count
from app.core.input_validation import (
    InputValidationError,
    normalize_reservation_name,
)
from app.utils.datetime_parser import DatetimeParser
from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
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
_PUBLIC_REFERENCE_TOKEN = r"RSV_[0-9A-Fa-f]{32}"
_PUBLIC_REFERENCE_CANDIDATE = re.compile(
    rf"(?<![0-9A-Za-z_]){_PUBLIC_REFERENCE_TOKEN}(?![0-9A-Za-z_])",
    re.IGNORECASE,
)
_PUBLIC_REFERENCE_WRAPPER = re.compile(
    rf"[ ]*(?:"
    rf"{_PUBLIC_REFERENCE_TOKEN}|"
    rf"referensi[ ]+reservasi[ ]*:[ ]*{_PUBLIC_REFERENCE_TOKEN}|"
    rf"reservasi[ ]+{_PUBLIC_REFERENCE_TOKEN}|"
    rf"pilih[ ]+{_PUBLIC_REFERENCE_TOKEN}|"
    rf"gunakan[ ]+referensi[ ]+{_PUBLIC_REFERENCE_TOKEN}"
    rf")[ ]*[.,!?;]?[ ]*",
    re.IGNORECASE,
)
_PUBLIC_REFERENCE_OUTER_PAIRS = {
    "(": ")",
    "[": "]",
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",
    "\u2018": "\u2019",
}
_PUBLIC_REFERENCE_OUTER_DELIMITERS = frozenset(
    (*_PUBLIC_REFERENCE_OUTER_PAIRS, *_PUBLIC_REFERENCE_OUTER_PAIRS.values())
)
_PUBLIC_REFERENCE_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_REFERENCE_LIKE = re.compile(r"(?<![0-9A-Za-z_])RSV_", re.IGNORECASE)
_ARBITRARY_IDENTIFIER = re.compile(r"\s*[A-Za-z0-9_-]+\s*")
REFERENCE_INPUT_GUIDANCE = (
    "Gunakan referensi reservasi dengan format RSV_ diikuti 32 karakter "
    "heksadesimal."
)
REFERENCE_AMBIGUITY_GUIDANCE = "Kirim tepat satu referensi reservasi."
REFERENCE_NOT_FOUND_RESPONSE = "Referensi reservasi tidak ditemukan."
REFERENCE_DATA_UNAVAILABLE_RESPONSE = (
    "Data reservasi belum dapat diproses dengan aman. Silakan coba lagi nanti."
)


class PublicReferenceParseStatus(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    MALFORMED = "malformed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, repr=False)
class PublicReferenceParseResult:
    status: PublicReferenceParseStatus
    reference: str | None = None

    def __post_init__(self) -> None:
        if self.status is PublicReferenceParseStatus.VALID:
            try:
                canonical = canonicalize_public_reference(self.reference)
            except InvalidPublicReservationReferenceError:
                raise ValueError("INVALID_PUBLIC_REFERENCE_PARSE_RESULT") from None
            object.__setattr__(self, "reference", canonical)
        elif self.reference is not None:
            raise ValueError("INVALID_PUBLIC_REFERENCE_PARSE_RESULT")

    def __repr__(self) -> str:
        return f"PublicReferenceParseResult(status={self.status.value!r})"


def normalize_natural_reservation_name(value: str) -> str:
    """Extract a strict stored name from a natural-language name candidate."""
    candidate = _NAME_FIELD_TRANSITION.split(value, maxsplit=1)[0]
    candidate = _TRAILING_SENTENCE_DELIMITER.sub("", candidate, count=1)
    return normalize_reservation_name(candidate)


def parse_public_reservation_reference(value: str) -> PublicReferenceParseResult:
    """Parse one bounded public reference without consulting persistence or AI."""

    if not isinstance(value, str) or not value.strip():
        return PublicReferenceParseResult(PublicReferenceParseStatus.MISSING)

    candidates = _PUBLIC_REFERENCE_CANDIDATE.findall(value)
    if len(candidates) > 1:
        return PublicReferenceParseResult(PublicReferenceParseStatus.AMBIGUOUS)
    if len(candidates) == 1:
        if _PUBLIC_REFERENCE_CONTROL_CHARACTER.search(value):
            return PublicReferenceParseResult(PublicReferenceParseStatus.MALFORMED)

        bounded = value.strip(" ")
        first = bounded[0]
        last = bounded[-1]
        if first in _PUBLIC_REFERENCE_OUTER_PAIRS:
            if last != _PUBLIC_REFERENCE_OUTER_PAIRS[first]:
                return PublicReferenceParseResult(
                    PublicReferenceParseStatus.MALFORMED
                )
            bounded = bounded[1:-1]
        elif (
            first in _PUBLIC_REFERENCE_OUTER_DELIMITERS
            or last in _PUBLIC_REFERENCE_OUTER_DELIMITERS
        ):
            return PublicReferenceParseResult(PublicReferenceParseStatus.MALFORMED)

        if _PUBLIC_REFERENCE_WRAPPER.fullmatch(bounded) is None:
            return PublicReferenceParseResult(PublicReferenceParseStatus.MALFORMED)
        try:
            canonical = canonicalize_public_reference(candidates[0])
        except InvalidPublicReservationReferenceError:
            return PublicReferenceParseResult(PublicReferenceParseStatus.MALFORMED)
        return PublicReferenceParseResult(
            PublicReferenceParseStatus.VALID,
            canonical,
        )

    if _REFERENCE_LIKE.search(value) or _ARBITRARY_IDENTIFIER.fullmatch(value):
        return PublicReferenceParseResult(PublicReferenceParseStatus.MALFORMED)
    return PublicReferenceParseResult(PublicReferenceParseStatus.MISSING)


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
