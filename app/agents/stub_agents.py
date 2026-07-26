from typing import Any


class BaseStubAgent:
    """Simple fixed-response agent for intents without a full workflow."""

    def __init__(self, response_template: str):
        self.response_template = response_template

    async def run(self, steps: list[dict[str, Any]], session_state: dict[str, Any], user_message: str, session_id: str | None = None) -> dict[str, Any]:
        return {
            "status": "stub",
            "response": self.response_template.format(user_message=user_message),
        }


class CheckReservationAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("Saya akan membantu mengecek reservasi Anda.")


class CancelReservationAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("Saya akan membantu membatalkan reservasi Anda.")


class GreetingAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("Halo! Saya AURA. Ada yang bisa saya bantu?")


class GeneralQuestionAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("Saya akan membantu menjawab pertanyaan Anda.")
