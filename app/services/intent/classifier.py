import json

from app.services.ai.factory import get_ai_provider


class IntentClassifier:

    async def classify(self, message: str):

        provider = get_ai_provider()

        prompt = f"""
Kamu adalah AI Intent Classifier.

Tugasmu memilih SATU intent.

Pilihan intent:

- reservation
- view_reservation
- menu
- promo
- faq
- complaint
- general

Jawab HANYA JSON.

Contoh:

{{
    "intent":"reservation"
}}

User:
{message}
"""

        response = await provider.chat(prompt)

        try:
            data = json.loads(response)
            return data.get("intent", "general")

        except Exception:
            return "general"
