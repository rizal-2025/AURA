"""Bounded date continuation for an already active reservation draft.

Only a day is retained; never infer owner, clock, or confirmation from text.
The caller owns session scope, durable publication and final domain validation.
"""

import re
from datetime import date

from app.utils.datetime_parser import DatetimeParser, MONTHS, current_local_date

PENDING_DAY = "pending_reservation_day"
_MONTH = "|".join(MONTHS)


def continue_date(text, state, *, clock=None):
    normalized = " ".join(text.casefold().strip().split()).strip(".,!?")
    day = re.fullmatch(r"(?:(?:ubah|ganti|edit|pindah)\s+)?(?:tanggal|date|day)\s+([0-9]{1,2})", normalized)
    if day and 1 <= int(day[1]) <= 31:
        state[PENDING_DAY] = int(day[1])
        return text
    pending = state.get(PENDING_DAY)
    rest = re.fullmatch(rf"({_MONTH})\s+([0-9]{{4}})", normalized)
    if type(pending) is int and rest:
        # Invalid calendar dates remain pending and recoverable; never clamp.
        try:
            complete = date(int(rest[2]), MONTHS[rest[1]], pending).isoformat()
        except ValueError:
            return text
        state.pop(PENDING_DAY, None)
        return complete
    if DatetimeParser.parse_date(text, clock=clock) is not None:
        state.pop(PENDING_DAY, None)
    return text


def inferred_year(text, *, clock=None):
    """Report next-year inference; explicit ISO/numeric/named years stay final."""
    explicit_named = rf"\b(?:[0-9]{{1,2}}\s+(?:{_MONTH})\s+[0-9]{{4}}|(?:{_MONTH})\s+[0-9]{{1,2}}(?:st|nd|rd|th)?[,]?\s+[0-9]{{4}})\b"
    if re.search(explicit_named, text, re.IGNORECASE):
        return None
    if not re.search(rf"\b(?:{_MONTH})\b", text, re.IGNORECASE):
        return None
    candidate = DatetimeParser.parse_date(text, clock=clock)
    if candidate and date.fromisoformat(candidate).year > current_local_date(clock=clock).year:
        return date.fromisoformat(candidate).year
    return None
