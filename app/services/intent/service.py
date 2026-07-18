from app.services.intent.classifier import IntentClassifier


class IntentService:

    def __init__(self):
        self.classifier = IntentClassifier()

    async def detect(self, message: str):
        return await self.classifier.classify(message)