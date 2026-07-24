from datetime import date, datetime, timedelta, timezone
import re

WEEKDAYS = {
    "senin": 0,
    "selasa": 1,
    "rabu": 2,
    "kamis": 3,
    "jumat": 4,
    "sabtu": 5,
    "minggu": 6,
}


class DatetimeParser:

    @staticmethod
    def parse_date(
        text: str,
        *,
        reference_date: date | None = None,
    ) -> str | None:
        text = text.lower()
        today = reference_date or datetime.now(
            timezone(timedelta(hours=7)),
        ).date()

        for day_name, weekday in WEEKDAYS.items():
            if day_name in text:
                days_ahead = weekday - today.weekday()

                if days_ahead <= 0:
                    days_ahead += 7

                target = today + timedelta(days=days_ahead)
                return target.strftime("%Y-%m-%d")

        if "hari ini" in text:
            return today.strftime("%Y-%m-%d")

        if "besok" in text:
            return (today + timedelta(days=1)).strftime("%Y-%m-%d")

        if "lusa" in text:
            return (today + timedelta(days=2)).strftime("%Y-%m-%d")

        return None

    @staticmethod
    def parse_time(text: str) -> str | None:
        text = text.lower()

        keywords = [
            "jam",
            "pukul",
            "malam",
            "pagi",
            "siang",
            "sore",
            "setengah",
        ]

        if not any(keyword in text for keyword in keywords):
            return None

        if "setengah" in text:
            match = re.search(r"setengah\s+(\w+)", text)
            if match:
                hour = 0
                word = match.group(1)
                if word in {"satu"}:
                    hour = 1
                elif word in {"dua"}:
                    hour = 2
                elif word in {"tiga"}:
                    hour = 3
                elif word in {"empat"}:
                    hour = 4
                elif word in {"lima"}:
                    hour = 5
                elif word in {"enam"}:
                    hour = 6
                elif word in {"tujuh"}:
                    hour = 7
                elif word in {"delapan"}:
                    hour = 8
                elif word in {"sembilan"}:
                    hour = 9
                elif word in {"sepuluh"}:
                    hour = 10
                elif word in {"sebelas"}:
                    hour = 11
                elif word in {"dua belas", "duabelas"}:
                    hour = 12
                else:
                    hour = 0

                if hour == 12:
                    base_hour = 0
                else:
                    base_hour = hour

                if "malam" in text and hour < 12:
                    base_hour = hour + 12 if hour < 12 else 0
                    if hour == 8:
                        base_hour = 19
                elif "sore" in text and hour < 12:
                    base_hour = hour + 12 if hour < 12 else 0
                elif "siang" in text and hour < 7:
                    base_hour = hour + 12 if hour < 12 else 0
                elif "pagi" in text:
                    base_hour = hour
                elif hour <= 7:
                    base_hour = hour + 12 if hour < 12 else 0
                else:
                    base_hour = hour

                return f"{base_hour:02d}:30"

        match = re.search(r"(?:jam\s*)?(\d{1,2})", text)
        if not match:
            return None

        hour = int(match.group(1))

        if "malam" in text and hour < 12:
            hour += 12
        elif "sore" in text and hour < 12:
            hour += 12
        elif "siang" in text and hour < 7:
            hour += 12
        elif "pagi" in text:
            hour = hour
        elif hour <= 7:
            hour += 12

        if "12" in text and "malam" in text:
            hour = 0

        return f"{hour:02d}:00"
