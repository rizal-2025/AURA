import re
from typing import Any


class ReservationEntityExtractor:
    """Extract reservation entities from a single user message."""

    async def extract(self, message: str) -> dict[str, Any]:
        text = message.lower()
        result: dict[str, Any] = {}

        name_match = re.search(r"atas nama\s+([a-zA-Z0-9\s]+)", text)
        if name_match:
            result["name"] = name_match.group(1).strip().title()

        people_match = re.search(r"(\d+)\s+orang", text)
        if people_match:
            result["people"] = int(people_match.group(1))

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
