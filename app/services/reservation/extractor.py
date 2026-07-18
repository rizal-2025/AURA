import json

from app.services.ai.factory import get_ai_provider
from app.utils.datetime_parser import DatetimeParser
from app.core.logger import logger

class ReservationExtractor:

    async def extract(
            self,
            message: str):

        provider = get_ai_provider()

        prompt = f"""
Kamu adalah AI Reservation Extractor.

Tugasmu adalah mengekstrak informasi reservasi dari pesan pengguna.

Field yang harus diekstrak:

- name
- people
- date
- time

Aturan:

1. Jawab HANYA dalam format JSON.
2. Jangan tambahkan penjelasan apa pun.
3. Jika suatu data tidak ditemukan, isi dengan null.
4. Jika user hanya menyebut satu informasi, isi hanya field tersebut.
5. Jangan mengarang data yang tidak disebutkan user.
6. Gunakan format tanggal YYYY-MM-DD.
7. Gunakan format waktu HH:MM (24 jam).

Contoh:

User:
Saya mau booking besok jam 7 malam untuk 6 orang atas nama Rizal

Jawaban:

{{
    "name": "Rizal",
    "people": 6,
    "date": "2026-07-16",
    "time": "19:00"
}}

Contoh:

User:
Nama saya Andi

Jawaban:

{{
    "name": "Andi",
    "people": null,
    "date": null,
    "time": null
}}

Contoh:

User:
6 orang

Jawaban:

{{
    "name": null,
    "people": 6,
    "date": null,
    "time": null
}}

Contoh:

User:
Besok jam 7 malam

Jawaban:

{{
    "name": null,
    "people": null,
    "date": "2026-07-16",
    "time": "19:00"
}}

Pesan pengguna:

{message}
"""

        response = await provider.chat(prompt)

        logger.info(f"RAW AI RESPONSE:\n{response}")

        try:
            result = json.loads(response)

            parsed_date = DatetimeParser.parse_date(message)
            if parsed_date:
                result["date"] = parsed_date

            parsed_time = DatetimeParser.parse_time(message)
            if parsed_time:
                result["time"] = parsed_time

            return result

        except Exception as e:

            logger.error(f"JSON ERROR: {e}")
            logger.error(f"RAW RESPONSE:\n{response}")

            return {
                "name": None,
                "people": None,
                "date": None,
                "time": None,
            }