from typing import Any


class MemoryManager:
    """Manage conversation state in memory using session_id as the key."""

    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}

    def create_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "intent": None,
                "name": None,
                "people": None,
                "date": None,
                "time": None,
                "completed": False,
                "editing_field": None,
            }
        return self._sessions[session_id]

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.create_session(session_id)

    def update_session(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        session = self.create_session(session_id)
        for key, value in data.items():
            if value is not None:
                session[key] = value
        return session

    def clear_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
