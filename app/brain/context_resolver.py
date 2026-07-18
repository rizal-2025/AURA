from typing import Any


class ContextResolver:
    """Resolve conversational references for partial updates in reservation flow."""

    def __init__(self):
        self.change_keywords = {"ganti", "ubah", "jadi", "ubah jadi", "ganti jadi"}

    def resolve(self, conversation_state: dict[str, Any], user_message: str, extracted: dict[str, Any]) -> dict[str, Any]:
        updated_state = dict(conversation_state)
        lowered = user_message.strip().lower()

        if not extracted:
            return updated_state

        if self._is_change_request(lowered):
            for field, value in extracted.items():
                if value is None:
                    continue
                if self._field_should_update(field, lowered):
                    normalized_value = value
                    if field == "time" and isinstance(value, str):
                        normalized_value = value
                    updated_state[field] = normalized_value
            return updated_state

        for field, value in extracted.items():
            if value is None:
                continue
            updated_state[field] = value
        return updated_state

    def _is_change_request(self, user_message: str) -> bool:
        return any(keyword in user_message for keyword in self.change_keywords)

    def _field_should_update(self, field: str, user_message: str) -> bool:
        if field == "time":
            return True
        if field in {"name", "people", "date"}:
            return any(token in user_message for token in [field, "nama", "orang", "hari", "tanggal"])
        return False
