"""Pure, shared input validation for HTTP and conversational boundaries."""

from __future__ import annotations

from datetime import date
import re
import unicodedata


MAX_SESSION_REFERENCE_CODEPOINTS = 128
MAX_CHAT_MESSAGE_CODEPOINTS = 4096
MAX_RESERVATION_NAME_CODEPOINTS = 100
MIN_RESERVATION_PEOPLE = 1
MAX_RESERVATION_PEOPLE = 20

CHAT_SESSION_ID_INVALID = "CHAT_SESSION_ID_INVALID"
CHAT_MESSAGE_INVALID = "CHAT_MESSAGE_INVALID"
CHAT_MESSAGE_EMPTY = "CHAT_MESSAGE_EMPTY"
CHAT_MESSAGE_TOO_LONG = "CHAT_MESSAGE_TOO_LONG"
CHAT_MESSAGE_UNSAFE = "CHAT_MESSAGE_UNSAFE"
RESERVATION_NAME_INVALID = "RESERVATION_NAME_INVALID"
RESERVATION_PEOPLE_INVALID = "RESERVATION_PEOPLE_INVALID"
RESERVATION_DATE_INVALID = "RESERVATION_DATE_INVALID"
RESERVATION_TIME_INVALID = "RESERVATION_TIME_INVALID"
EXTRA_FIELD_FORBIDDEN = "EXTRA_FIELD_FORBIDDEN"
REQUEST_JSON_INVALID = "REQUEST_JSON_INVALID"
INPUT_INVALID = "INPUT_INVALID"

SAFE_INPUT_CODES = frozenset(
    {
        CHAT_SESSION_ID_INVALID,
        CHAT_MESSAGE_INVALID,
        CHAT_MESSAGE_EMPTY,
        CHAT_MESSAGE_TOO_LONG,
        CHAT_MESSAGE_UNSAFE,
        RESERVATION_NAME_INVALID,
        RESERVATION_PEOPLE_INVALID,
        RESERVATION_DATE_INVALID,
        RESERVATION_TIME_INVALID,
        EXTRA_FIELD_FORBIDDEN,
        REQUEST_JSON_INVALID,
        INPUT_INVALID,
    }
)

_SESSION_REFERENCE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_CANONICAL_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CANONICAL_TIME_PATTERN = re.compile(r"^[0-9]{2}:[0-9]{2}$")
_ALLOWED_NAME_PUNCTUATION = frozenset({" ", "'", "\u2019", "-", ".", "&"})


class InputValidationError(ValueError):
    """Validation failure that retains only an allowlisted stable code."""

    def __init__(self, code: str):
        self.code = code if code in SAFE_INPUT_CODES else INPUT_INVALID
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"InputValidationError({self.code!r})"


def _has_unsafe_unicode_other(value: str, *, allow_lf: bool = False) -> bool:
    for character in value:
        if allow_lf and character == "\n":
            continue
        if unicodedata.category(character).startswith("C"):
            return True
    return False


def validate_session_reference(value) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_SESSION_REFERENCE_CODEPOINTS
        or _SESSION_REFERENCE_PATTERN.fullmatch(value) is None
    ):
        raise InputValidationError(CHAT_SESSION_ID_INVALID)
    return value


def normalize_chat_message(value) -> str:
    if not isinstance(value, str):
        raise InputValidationError(CHAT_MESSAGE_INVALID)

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or normalized.isspace():
        raise InputValidationError(CHAT_MESSAGE_EMPTY)
    if len(normalized) > MAX_CHAT_MESSAGE_CODEPOINTS:
        raise InputValidationError(CHAT_MESSAGE_TOO_LONG)
    if _has_unsafe_unicode_other(normalized, allow_lf=True):
        raise InputValidationError(CHAT_MESSAGE_UNSAFE)
    return normalized


def normalize_reservation_name(value) -> str:
    if not isinstance(value, str):
        raise InputValidationError(RESERVATION_NAME_INVALID)

    normalized = unicodedata.normalize("NFC", value)
    if _has_unsafe_unicode_other(normalized):
        raise InputValidationError(RESERVATION_NAME_INVALID)
    if any(character.isspace() and character != " " for character in normalized):
        raise InputValidationError(RESERVATION_NAME_INVALID)

    normalized = re.sub(r" +", " ", normalized.strip(" "))
    if not 1 <= len(normalized) <= MAX_RESERVATION_NAME_CODEPOINTS:
        raise InputValidationError(RESERVATION_NAME_INVALID)

    has_letter_or_digit = False
    for character in normalized:
        category = unicodedata.category(character)
        if (
            category.startswith("L")
            or category.startswith("M")
            or category == "Nd"
            or character in _ALLOWED_NAME_PUNCTUATION
        ):
            if category.startswith("L") or category == "Nd":
                has_letter_or_digit = True
            continue
        raise InputValidationError(RESERVATION_NAME_INVALID)
    if not has_letter_or_digit:
        raise InputValidationError(RESERVATION_NAME_INVALID)
    return normalized


def validate_reservation_people(value) -> int:
    if (
        type(value) is not int
        or not MIN_RESERVATION_PEOPLE <= value <= MAX_RESERVATION_PEOPLE
    ):
        raise InputValidationError(RESERVATION_PEOPLE_INVALID)
    return value


def validate_reservation_date(value) -> str:
    if (
        not isinstance(value, str)
        or _CANONICAL_DATE_PATTERN.fullmatch(value) is None
    ):
        raise InputValidationError(RESERVATION_DATE_INVALID)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise InputValidationError(RESERVATION_DATE_INVALID) from None
    if parsed.isoformat() != value:
        raise InputValidationError(RESERVATION_DATE_INVALID)
    return value


def validate_reservation_time(value) -> str:
    if (
        not isinstance(value, str)
        or _CANONICAL_TIME_PATTERN.fullmatch(value) is None
    ):
        raise InputValidationError(RESERVATION_TIME_INVALID)
    hour = int(value[:2])
    minute = int(value[3:])
    if hour > 23 or minute > 59:
        raise InputValidationError(RESERVATION_TIME_INVALID)
    return value


def validate_reservation_field(field_name: str, value):
    validators = {
        "name": normalize_reservation_name,
        "people": validate_reservation_people,
        "date": validate_reservation_date,
        "time": validate_reservation_time,
    }
    validator = validators.get(field_name)
    if validator is None:
        raise InputValidationError(INPUT_INVALID)
    return validator(value)
