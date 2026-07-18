import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.brain.classifier import IntentClassifier


class TestBrainIntentClassifier(unittest.TestCase):
    def test_classify_returns_intent_and_confidence(self):
        classifier = IntentClassifier()
        classifier.ai = type("DummyAI", (), {"chat": AsyncMock(return_value='{"intent": "reservation", "confidence": 0.95}')})()

        result = asyncio.run(classifier.classify("Saya mau reservasi"))

        self.assertEqual(result["intent"], "reservation")
        self.assertEqual(result["confidence"], 0.95)


if __name__ == "__main__":
    unittest.main()
