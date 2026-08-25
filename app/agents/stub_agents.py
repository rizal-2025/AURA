from typing import Any

from app.core.locale import tr


class BaseStubAgent:
    """Simple fixed-response agent for intents without a full workflow."""

    def __init__(self, response_key: str):
        self.response_key = response_key

    async def run(self, steps: list[dict[str, Any]], session_state: dict[str, Any], user_message: str, session_id: str | None = None) -> dict[str, Any]:
        return {
            "status": "stub",
            "response": tr(self.response_key, user_message=user_message),
        }


class CheckReservationAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("check_reservation_help")


class CancelReservationAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("cancel_reservation_help")


class GreetingAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("greeting")


class GeneralQuestionAgent(BaseStubAgent):
    def __init__(self):
        super().__init__("general_help")
