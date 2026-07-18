from typing import Any


class Planner:
    """Create an execution plan from intent and conversation state."""

    def __init__(self):
        self._strategies = {
            "reservation": self._reservation_plan,
            "menu": self._menu_plan,
            "promo": self._promo_plan,
            "faq": self._faq_plan,
            "complaint": self._complaint_plan,
            "general": self._general_plan,
        }

    async def plan(self, intent_result: dict[str, Any], conversation_state: dict[str, Any]) -> dict[str, Any]:
        intent = intent_result.get("intent", "general")
        confidence = intent_result.get("confidence", 0.0)

        strategy = self._strategies.get(intent, self._general_plan)
        return {
            "intent": intent,
            "confidence": confidence,
            "steps": strategy(conversation_state),
        }

    def _reservation_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "reservation",
                "action": "collect_missing_fields",
                "fields": self._missing_fields(conversation_state),
            },
            {
                "agent": "reservation",
                "action": "save_reservation",
                "condition": "all_required_fields_collected",
            },
        ]

    def _menu_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "menu",
                "action": "answer_menu_query",
                "context": conversation_state,
            }
        ]

    def _promo_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "promo",
                "action": "answer_promo_query",
                "context": conversation_state,
            }
        ]

    def _faq_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "faq",
                "action": "answer_faq_query",
                "context": conversation_state,
            }
        ]

    def _complaint_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "complaint",
                "action": "handle_complaint",
                "context": conversation_state,
            }
        ]

    def _general_plan(self, conversation_state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent": "general",
                "action": "respond_general_help",
                "context": conversation_state,
            }
        ]

    def _missing_fields(self, conversation_state: dict[str, Any]) -> list[str]:
        required_fields = ["name", "people", "date", "time"]
        return [field for field in required_fields if not conversation_state.get(field)]
