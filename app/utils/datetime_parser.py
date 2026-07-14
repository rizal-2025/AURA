from datetime import datetime, timedelta
import re


class DatetimeParser:

    @staticmethod
    def parse_date(text: str) -> str | None:
        text = text.lower()

        today = datetime.today()

        if "besok" in text:
            return (today + timedelta(days=1)).strftime("%Y-%m-%d")

        if "lusa" in text:
            return (today + timedelta(days=2)).strftime("%Y-%m-%d")

        return None

    @staticmethod
    def parse_time(text: str) -> str | None:
        text = text.lower()

        match = re.search(r"jam\s*(\d{1,2})", text)

        if not match:
            return None

        hour = int(match.group(1))

        if "malam" in text and hour < 12:
            hour += 12

        if "sore" in text and hour < 12:
            hour += 12

        return f"{hour:02d}:00"