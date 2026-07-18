from typing import Any, Protocol


class LongTermMemoryStore(Protocol):
    def get_profile(self, user_id: str) -> dict[str, Any]:
        ...

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        ...

    def update_profile(self, user_id: str, updates: dict[str, Any]) -> None:
        ...

    def clear_profile(self, user_id: str) -> None:
        ...


class InMemoryLongTermMemoryStore:
    """Simple in-memory implementation that can later be replaced with Redis or vector DB."""

    def __init__(self):
        self._profiles: dict[str, dict[str, Any]] = {}

    def get_profile(self, user_id: str) -> dict[str, Any]:
        return dict(self._profiles.get(user_id, {}))

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        self._profiles[user_id] = dict(profile)

    def update_profile(self, user_id: str, updates: dict[str, Any]) -> None:
        profile = self.get_profile(user_id)
        for key, value in updates.items():
            if value is not None:
                profile[key] = value
        self._profiles[user_id] = profile

    def clear_profile(self, user_id: str) -> None:
        self._profiles.pop(user_id, None)


class LongTermMemoryManager:
    """High-level service for storing and retrieving user preferences."""

    def __init__(self, store: LongTermMemoryStore | None = None):
        self.store = store or InMemoryLongTermMemoryStore()

    def get_profile(self, user_id: str) -> dict[str, Any]:
        return self.store.get_profile(user_id)

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        self.store.save_profile(user_id, profile)

    def update_profile(self, user_id: str, updates: dict[str, Any]) -> None:
        self.store.update_profile(user_id, updates)

    def clear_profile(self, user_id: str) -> None:
        self.store.clear_profile(user_id)

    def merge_preferences(self, user_id: str, preferences: dict[str, Any]) -> dict[str, Any]:
        profile = self.get_profile(user_id)
        merged = dict(profile)
        for key, value in preferences.items():
            if value is not None:
                merged[key] = value
        self.save_profile(user_id, merged)
        return merged

    def suggest_context(self, user_id: str) -> dict[str, Any]:
        profile = self.get_profile(user_id)
        return {
            "favorite_name": profile.get("favorite_name"),
            "preferred_people": profile.get("preferred_people"),
            "favorite_time": profile.get("favorite_time"),
            "favorite_table": profile.get("favorite_table"),
        }
