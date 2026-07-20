from typing import Any


class MemoryManager:
    """Manage conversation state using an internal, caller-provided key.

    Authenticated API callers provide an owner-scoped key. This class does not
    derive identity from client input and must not log its keys.
    """

    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}

    def create_session(self, memory_key: str) -> dict[str, Any]:
        if memory_key not in self._sessions:
            self._sessions[memory_key] = {
                "intent": None,
                "name": None,
                "people": None,
                "date": None,
                "time": None,
                "completed": False,
                "editing_field": None,
            }
        return self._sessions[memory_key]

    def get_session(self, memory_key: str) -> dict[str, Any]:
        return self.create_session(memory_key)

    def update_session(self, memory_key: str, data: dict[str, Any]) -> dict[str, Any]:
        session = self.create_session(memory_key)
        for key, value in data.items():
            if value is not None:
                session[key] = value
        return session

    def clear_session(self, memory_key: str) -> None:
        self._sessions.pop(memory_key, None)
