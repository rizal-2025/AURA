import json

from app.services.ai.factory import get_ai_provider
from app.utils.datetime_parser import DatetimeParser


class ReservationExtractor:

    async def extract(self, message: str):

        provider = get_ai_provider()

        prompt = f"""
Kamu adalah AI Reservation Extractor.

Ekstrak informasi berikut:

- name
- people
- date
- time

Jawab HANYA JSON.

Contoh:

{{
    "name":"Rizal",
    "people":6,
    "date":"2026-07-14",
    "time":"19:00"
}}

Jika data tidak ada,
isi null.

User:

{message}
"""

        response = await provider.chat(prompt)

        try:
            result = json.loads(response)

            if result.get("date"):
                parsed_date = DatetimeParser.parse_date(message)
                if parsed_date:
                    result["date"] = parsed_date

            if result.get("time"):
                parsed_time = DatetimeParser.parse_time(message)
                if parsed_time:
                    result["time"] = parsed_time

            return result

        except Exception:
            return {
                "name": None,
                "people": None,
                "date": None,
                "time": None,
            }