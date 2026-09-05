from collections.abc import Callable
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "senin": 0,
    "selasa": 1,
    "rabu": 2,
    "kamis": 3,
    "jumat": 4,
    "sabtu": 5,
    "minggu": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
    "november": 11,
    "december": 12,
}

NUMBER_HOURS = {
    "satu": 1,
    "dua": 2,
    "tiga": 3,
    "empat": 4,
    "lima": 5,
    "enam": 6,
    "tujuh": 7,
    "delapan": 8,
    "sembilan": 9,
    "sepuluh": 10,
    "sebelas": 11,
    "dua belas": 12,
    "duabelas": 12,
}

NUMBER_MINUTES = {
    "lima belas": 15,
    "tiga puluh": 30,
}

JAKARTA_TIMEZONE = ZoneInfo("Asia/Jakarta")


def current_local_datetime(
    *,
    clock: Callable[[], datetime] | None = None,
) -> datetime:
    """Return the authoritative application time in Jakarta."""

    now = clock() if clock is not None else datetime.now(JAKARTA_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=JAKARTA_TIMEZONE)
    return now.astimezone(JAKARTA_TIMEZONE)


def current_local_date(
    *,
    clock: Callable[[], datetime] | None = None,
) -> date:
    """Return the authoritative application calendar date in Jakarta."""

    return current_local_datetime(clock=clock).date()


class DatetimeParser:

    @staticmethod
    def parse_date(
        text: str,
        *,
        reference_date: date | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> str | None:
        if not isinstance(text, str):
            return None
        normalized = " ".join(text.casefold().strip().split())
        today = reference_date or current_local_date(clock=clock)

        for numeric in re.finditer(
            r"(?<![0-9])([0-9]{1,2})([/\-])[0-9]{1,2}\2([0-9][a-z0-9]*)\b", normalized
        ):
            if not (len(numeric[3]) == 4 and numeric[3].isdigit() and int(numeric[3]) > 0):
                return None
        month_names = "|".join(MONTHS)
        named_dates = list(re.finditer(
            rf"(?<![0-9])([0-9]{{1,2}})\s+({month_names})\b",
            normalized,
        ))
        english_dates = [match for match in re.finditer(
            rf"\b(?:{month_names})\s+[0-9]{{1,2}}(?:st|nd|rd|th)?\b", normalized)
            if not any(named.start() <= match.start() < named.end() for named in named_dates)]
        for match in named_dates + english_dates:
            if DatetimeParser._malformed_named_year(normalized[match.end():]):
                return None
        other_dates = re.finditer(
            r"\b(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}(?:/[0-9]{1,2}/|-[0-9]{1,2}-)[0-9]{4})\b", normalized)
        absolute_count = len(named_dates) + len(english_dates) + sum(1 for _ in other_dates)
        relative = re.findall(rf"\b(?:hari ini|today|besok|tomorrow|lusa|{'|'.join(WEEKDAYS)})\b", normalized)
        if absolute_count > 1 or len(relative) > 1 or (absolute_count and relative):
            return None

        for day_name, weekday in WEEKDAYS.items():
            day_match = re.search(
                rf"\b(?:hari\s+)?{day_name}(?:\s+(ini|depan))?\b",
                normalized,
            )
            if day_match:
                days_ahead = weekday - today.weekday()
                modifier = day_match.group(1)
                if modifier == "ini":
                    if days_ahead < 0:
                        return None
                elif days_ahead <= 0:
                    days_ahead += 7
                target = today + timedelta(days=days_ahead)
                return target.isoformat()

        if re.search(r"\b(?:hari ini|today)\b", normalized):
            return today.isoformat()

        if re.search(r"\b(?:besok|tomorrow)\b", normalized):
            return (today + timedelta(days=1)).isoformat()

        if re.search(r"\blusa\b", normalized):
            return (today + timedelta(days=2)).isoformat()

        iso_match = re.search(r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])", normalized)
        if iso_match:
            return DatetimeParser._valid_date(
                *map(int, iso_match.group(1).split("-")),
            )

        numeric_match = re.search(
            r"(?<![0-9])([0-9]{1,2})([/\-])([0-9]{1,2})\2([0-9]{4})(?![0-9])",
            normalized,
        )
        if numeric_match:
            day, month, year = (
                int(numeric_match.group(1)),
                int(numeric_match.group(3)),
                int(numeric_match.group(4)),
            )
            return DatetimeParser._valid_date(year, month, day)

        month_names = "|".join(MONTHS)
        named_match = re.search(
            rf"(?<![0-9])([0-9]{{1,2}})\s+({month_names})(?:\s+([0-9]{{4}}))?\b",
            normalized,
        )
        if named_match:
            day = int(named_match.group(1))
            month = MONTHS[named_match.group(2)]
            explicit_year = named_match.group(3)
            if explicit_year:
                return DatetimeParser._valid_date(
                    int(explicit_year),
                    month,
                    day,
                )
            candidate = DatetimeParser._valid_date(today.year, month, day)
            if candidate is None:
                return None
            if date.fromisoformat(candidate) < today:
                candidate = DatetimeParser._valid_date(today.year + 1, month, day)
            return candidate

        english_named_match = re.search(
            rf"\b({month_names})\s+([0-9]{{1,2}})(?:st|nd|rd|th)?(?:,?\s+([0-9]{{4}}))?\b",
            normalized,
        )
        if english_named_match:
            month = MONTHS[english_named_match.group(1)]
            day = int(english_named_match.group(2))
            explicit_year = english_named_match.group(3)
            year = int(explicit_year) if explicit_year else today.year
            candidate = DatetimeParser._valid_date(year, month, day)
            if candidate is not None and date.fromisoformat(candidate) < today and not explicit_year:
                candidate = DatetimeParser._valid_date(year + 1, month, day)
            return candidate

        return None

    @staticmethod
    def parse_time(text: str) -> str | None:
        if not isinstance(text, str):
            return None
        normalized = " ".join(text.casefold().strip().split())
        # Conflicting alternatives must not silently choose the first clock.
        if len(re.findall(r"\b(?:pagi|siang|sore|malam)\b", normalized)) > 1:
            return None
        if len(re.findall(r"(?<![0-9])[0-9]{1,2}[:.][0-9]{2}(?![0-9])", normalized)) > 1:
            return None
        period = r"(?:pagi|siang|sore|malam|a\.?m\.?|p\.?m\.?)"
        hours = "|".join(sorted(NUMBER_HOURS, key=len, reverse=True))
        clocks = list(re.finditer(
            rf"(?<![0-9:.])(?:[0-9]{{1,2}}[:.][0-9]+(?:\s*{period}\b)?|(?:{hours}|[0-9]{{1,2}})\s*{period}\b)", normalized)
        )
        if len(clocks) > 1:
            return None
        # English auxiliary 'am' is not a clock. Qualifiers count only when
        # attached to a clock; a second qualifier on that clock is conflicting.
        if clocks and re.match(rf"\s*{period}\b", normalized[clocks[0].end():]):
            return None
        numeric_clock = re.search(r"(?<![0-9])[0-9]+[:.]([0-9]+)", normalized)
        if numeric_clock and len(numeric_clock[1]) != 2:
            return None

        canonical_match = re.search(
            r"(?<![0-9:.])([0-9]{1,2})[:.]([0-9]{2})(?![0-9:.])"
            r"(?:\s*(a\.?m\.?|p\.?m\.?|pagi|siang|sore|malam)\b)?",
            normalized,
        )
        if canonical_match:
            hour, minute = map(int, canonical_match.group(1, 2))
            period = (canonical_match.group(3) or "").replace(".", "")
            if hour > 23 or minute > 59:
                return None
            if period in {"am", "pm"}:
                if not 1 <= hour <= 12:
                    return None
                hour = hour % 12 + (12 if period == "pm" else 0)
            elif period:
                hour = DatetimeParser._qualify_hour(hour, period)
                if hour is None:
                    return None
            return f"{hour:02d}:{minute:02d}"

        english_clock_match = re.search(
            r"(?<![0-9])([0-9]{1,2})(?::([0-5][0-9]))?\s*(a\.?m\.?|p\.?m\.?)\b",
            normalized,
        )
        if english_clock_match:
            hour = int(english_clock_match.group(1))
            if not 1 <= hour <= 12:
                return None
            minute = int(english_clock_match.group(2) or 0)
            period = english_clock_match.group(3).replace(".", "")
            converted = hour % 12 + (12 if period == "pm" else 0)
            return f"{converted:02d}:{minute:02d}"

        qualifier_match = re.search(r"\b(pagi|siang|sore|malam)\b", normalized)
        qualifier = qualifier_match.group(1) if qualifier_match else None

        half_pattern = "|".join(
            sorted((re.escape(word) for word in NUMBER_HOURS), key=len, reverse=True)
        )
        minute_pattern = "|".join(
            sorted(
                (re.escape(word) for word in NUMBER_MINUTES),
                key=len,
                reverse=True,
            )
        )
        half_match = re.search(
            rf"\bsetengah\s+({half_pattern}|[0-9]{{1,2}})\b",
            normalized,
        )
        if half_match:
            stated_hour = DatetimeParser._hour_value(half_match.group(1))
            if stated_hour is None or qualifier is None:
                return None
            base_hour = 11 if stated_hour == 12 else stated_hour - 1
            converted = DatetimeParser._qualify_hour(base_hour, qualifier)
            return f"{converted:02d}:30" if converted is not None else None

        bare_qualified_match = re.search(
            rf"(?<![0-9])({half_pattern}|[0-9]{{1,2}})\s+"
            r"(pagi|siang|sore|malam)\b",
            normalized,
        )
        if bare_qualified_match:
            hour = DatetimeParser._hour_value(bare_qualified_match.group(1))
            if hour is None:
                return None
            converted = DatetimeParser._qualify_hour(
                hour,
                bare_qualified_match.group(2),
            )
            return f"{converted:02d}:00" if converted is not None else None

        clock_match = re.search(
            rf"\b(?:jam(?:nya)?|pukul)\s+(?:jadi\s+)?({half_pattern}|[0-9]{{1,2}})\b",
            normalized,
        )
        if not clock_match:
            return None
        hour = DatetimeParser._hour_value(clock_match.group(1))
        if hour is None:
            return None
        minute_match = re.search(
            rf"\blewat\s+({minute_pattern}|[0-9]{{1,2}})\b",
            normalized[clock_match.end():],
        )
        if minute_match:
            minute_text = minute_match.group(1)
            minute = (
                int(minute_text)
                if minute_text.isdigit()
                else NUMBER_MINUTES.get(minute_text)
            )
            if minute is None or not 0 <= minute <= 59:
                return None
        else:
            minute = 0
        if qualifier is None:
            # A stated "lewat" minute is treated as a precise clock reading;
            # a bare "jam 7" remains ambiguous and is clarified separately.
            if minute_match is None:
                return None
            converted = hour
        else:
            converted = DatetimeParser._qualify_hour(hour, qualifier)
            if converted is None:
                return None
        return f"{converted:02d}:{minute:02d}"

    @staticmethod
    def _malformed_named_year(tail: str) -> bool:
        """Inspect only the token attached to a named date, in either order.

        Independent clocks/party sizes are not years. Other numeric-leading
        tokens must be a complete four-digit year, never an inference fallback.
        """
        token = re.match(r",?\s+([0-9][a-z0-9]*)", tail)
        if token is None:
            return False
        if re.match(r",?\s+[0-9]{1,2}[:.][0-9]{2}(?![0-9:.])", tail):
            return False
        if re.match(r",?\s+[0-9]+\s+(?:pagi|siang|sore|malam|am|pm|orang|people|persons|guests)\b", tail):
            return False
        return not (len(token[1]) == 4 and token[1].isascii()
                    and token[1].isdigit() and int(token[1]) > 0)

    @staticmethod
    def date_ambiguity(text: str) -> str | None:
        normalized = " ".join(text.casefold().strip().split())
        if re.search(r"\btanggal\s+[0-9]{1,2}\b", normalized):
            return "missing_month_year"
        return None

    @staticmethod
    def time_ambiguity(text: str) -> str | None:
        normalized = " ".join(text.casefold().strip().split())
        if " lewat " in f" {normalized} ":
            return None
        if re.search(
            r"\b(?:jam(?:nya)?|pukul)\s+(?:jadi\s+)?(?:[0-9]{1,2}|[a-z]+)\b",
            normalized,
        ):
            if not re.search(r"\b(?:pagi|siang|sore|malam)\b", normalized):
                return "missing_day_period"
        return None

    @staticmethod
    def _valid_date(year: int, month: int, day: int) -> str | None:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _hour_value(value: str) -> int | None:
        if value.isdigit():
            hour = int(value)
            return hour if 1 <= hour <= 12 else None
        return NUMBER_HOURS.get(value)

    @staticmethod
    def _qualify_hour(hour: int, qualifier: str) -> int | None:
        if not 0 <= hour <= 12:
            return None
        if qualifier == "pagi":
            return 0 if hour == 12 else hour
        if qualifier == "siang":
            # Indonesian noon is not an alias for English PM: 11 siang is
            # 11:00, whereas 1/2/3 siang are afternoon hours.
            return {11: 11, 12: 12, 1: 13, 2: 14, 3: 15}.get(hour)
        if qualifier == "sore":
            return hour + 12 if 3 <= hour <= 7 else None
        if qualifier == "malam":
            if hour == 12:
                return 0
            return hour + 12 if 6 <= hour <= 11 else None
        return None
