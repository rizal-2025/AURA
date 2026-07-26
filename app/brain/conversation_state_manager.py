from typing import Any


class ConversationStateManager:
    """Reasoning layer for reservation conversation progress."""

    REQUIRED_FIELDS = ["name", "people", "date", "time"]
    FIELD_QUESTIONS = {
        "name": "Atas nama siapa reservasinya?",
        "people": "Untuk berapa orang?",
        "date": "Tanggal berapa?",
        "time": "Jam berapa?",
    }

    def get_next_action(self, conversation_state: dict[str, Any]) -> dict[str, Any]:
        if conversation_state.get("completed"):
            return {"next_action": "complete", "field": None, "question": None}

        asked_fields = set(conversation_state.get("asked_fields", []))
        for field in self.REQUIRED_FIELDS:
            if field in asked_fields:
                continue
            if not conversation_state.get(field):
                return {
                    "next_action": self._action_for_field(field),
                    "field": field,
                    "question": self.FIELD_QUESTIONS[field],
                }

        if self._all_required_fields_present(conversation_state):
            return {"next_action": "confirm", "field": None, "question": None}

        return {"next_action": "complete", "field": None, "question": None}

    def record_question(self, conversation_state: dict[str, Any], field: str) -> dict[str, Any]:
        next_state = dict(conversation_state)
        asked_fields = list(next_state.get("asked_fields", []))
        # Persisted workflow snapshots require a stable prefix.  One-shot NLU
        # can prefill earlier fields, so record every field through the one
        # currently being asked rather than producing a sparse sequence.
        if field in self.REQUIRED_FIELDS:
            field_index = self.REQUIRED_FIELDS.index(field)
            asked_fields = list(self.REQUIRED_FIELDS[: field_index + 1])
        next_state["asked_fields"] = asked_fields
        return next_state

    def _all_required_fields_present(self, conversation_state: dict[str, Any]) -> bool:
        return all(conversation_state.get(field) for field in self.REQUIRED_FIELDS)

    def _action_for_field(self, field: str) -> str:
        return {
            "name": "ask_name",
            "people": "ask_people",
            "date": "ask_date",
            "time": "ask_time",
        }.get(field, "ask_info")
