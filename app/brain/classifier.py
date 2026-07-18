import json
from typing import Any

from app.services.ai.factory import get_ai_provider


class IntentClassifier:
    """Classify user message into an intent with optional confidence score."""

    DEFAULT_INTENT = "general"
    DEFAULT_CONFIDENCE = 0.0

    def __init__(self, provider: Any | None = None):
        self.ai = provider or get_ai_provider()

    async def classify(self, message: str) -> dict[str, Any]:
        prompt = self._build_prompt(message)
        response = await self.ai.chat(prompt)

        try:
            data = json.loads(response)
            intent = data.get("intent", self.DEFAULT_INTENT)
            confidence = data.get("confidence", self.DEFAULT_CONFIDENCE)
            return {
                "intent": intent,
                "confidence": confidence,
            }
        except Exception:
            return {
                "intent": self.DEFAULT_INTENT,
                "confidence": self.DEFAULT_CONFIDENCE,
            }

    def _build_prompt(self, message: str) -> str:
        intents = self._get_supported_intents()
        return f"""
Kamu adalah Intent Classifier untuk AURA.

Tugasmu adalah memilih satu intent dari daftar berikut:
{intents}

Aturan:
1. Jawab hanya dalam format JSON.
2. Format harus:
{{"intent": "reservation", "confidence": 0.95}}
3. Jika tidak yakin, gunakan intent general.
4. Jangan tambahkan teks lain.

Pesan user:
{message}
"""

    def _get_supported_intents(self) -> list[str]:
        return [
            "reservation",
            "menu",
            "promo",
            "faq",
            "complaint",
            "general",
        ]
