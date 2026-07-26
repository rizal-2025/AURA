from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.memory_errors import (
    ConversationMemoryValidationError,
    ReservationMutationGuardError,
)


@dataclass(frozen=True)
class _FrozenList:
    values: tuple[Any, ...]


@dataclass(frozen=True)
class _FrozenDictionary:
    items: tuple[tuple[str, Any], ...]


_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "bearer_token",
        "database_url",
        "jwt_secret",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)

CONVERSATION_SNAPSHOT_MAX_DEPTH = 16
CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS = 256
CONVERSATION_SNAPSHOT_MAX_TOTAL_NODES = 2048
_RESERVATION_GUARD_STATUSES = frozenset(
    {
        "outcome_unknown",
        "session_unusable",
        "committed_memory_unavailable",
    }
)
_RESERVATION_GUARD_OPERATIONS = frozenset({"create", "update", "cancel"})


def _normalized_key(key: str) -> str:
    return unicodedata.normalize("NFKC", key).casefold()


def _freeze_safe_value(
    value: object,
    *,
    depth: int = 0,
    active_containers: set[int] | None = None,
    node_count: list[int] | None = None,
) -> Any:
    if depth > CONVERSATION_SNAPSHOT_MAX_DEPTH:
        raise ConversationMemoryValidationError()
    if active_containers is None:
        active_containers = set()
    if node_count is None:
        node_count = [0]
    node_count[0] += 1
    if node_count[0] > CONVERSATION_SNAPSHOT_MAX_TOTAL_NODES:
        raise ConversationMemoryValidationError()

    if value is None or type(value) in (bool, str):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ConversationMemoryValidationError()
        return value
    if type(value) not in (list, dict):
        raise ConversationMemoryValidationError()
    if len(value) > CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS:
        raise ConversationMemoryValidationError()

    container_id = id(value)
    if container_id in active_containers:
        raise ConversationMemoryValidationError()
    active_containers.add(container_id)
    try:
        if type(value) is list:
            return _FrozenList(
                tuple(
                    _freeze_safe_value(
                        item,
                        depth=depth + 1,
                        active_containers=active_containers,
                        node_count=node_count,
                    )
                    for item in value
                )
            )

        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            if (
                type(key) is not str
                or _normalized_key(key) in _SENSITIVE_KEYS
            ):
                raise ConversationMemoryValidationError()
            items.append(
                (
                    key,
                    _freeze_safe_value(
                        item,
                        depth=depth + 1,
                        active_containers=active_containers,
                        node_count=node_count,
                    ),
                )
            )
        return _FrozenDictionary(tuple(items))
    finally:
        active_containers.remove(container_id)


def _thaw_safe_value(value: Any) -> Any:
    if isinstance(value, _FrozenList):
        return [_thaw_safe_value(item) for item in value.values]
    if isinstance(value, _FrozenDictionary):
        return {
            key: _thaw_safe_value(item)
            for key, item in value.items
        }
    return value


class ConversationSnapshot:
    """Immutable snapshot of one conversation.

    The stored tree contains only recursively frozen JSON-like values.
    ``materialize`` always returns a new mutable tree and never exposes the
    snapshot's private representation.
    """

    __slots__ = ("__state",)

    def __init__(self, state: Mapping[str, object]):
        if not isinstance(state, Mapping):
            raise ConversationMemoryValidationError()
        if type(state) is not dict:
            copied_state: dict[str, object] = {}
            mapping_failed = False
            try:
                for index, (key, value) in enumerate(state.items(), start=1):
                    if index > CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS:
                        raise ConversationMemoryValidationError()
                    copied_state[key] = value
            except ConversationMemoryValidationError:
                raise
            except Exception:
                mapping_failed = True
            if mapping_failed:
                raise ConversationMemoryValidationError() from None
            state = copied_state
        frozen = _freeze_safe_value(state)
        if not isinstance(frozen, _FrozenDictionary):
            raise ConversationMemoryValidationError()
        object.__setattr__(self, "_ConversationSnapshot__state", frozen)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ConversationSnapshot is immutable")

    def materialize(self) -> dict[str, Any]:
        state = _thaw_safe_value(self.__state)
        if not isinstance(state, dict):
            raise ConversationMemoryValidationError()
        return state


class MemoryManager:
    """Manage conversation state using an internal, caller-provided key.

    Authenticated API callers provide an owner-scoped key. This class does not
    derive identity from client input and must not log its keys.
    """

    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}
        self._reservation_mutation_guards: dict[str, dict[str, str]] = {}

    @staticmethod
    def _default_conversation_state() -> dict[str, Any]:
        return {
            "intent": None,
            "name": None,
            "people": None,
            "date": None,
            "time": None,
            "completed": False,
            "editing_field": None,
        }

    def create_session(self, memory_key: str) -> dict[str, Any]:
        if memory_key not in self._sessions:
            self._sessions[memory_key] = self._default_conversation_state()
        return self._sessions[memory_key]

    def get_session(self, memory_key: str) -> dict[str, Any]:
        return self.create_session(memory_key)

    def snapshot_conversation(self, memory_key: str) -> ConversationSnapshot:
        """Capture one conversation as an isolated immutable value tree.

        Authenticated callers are responsible for holding the existing
        per-conversation G1C lock. This method deliberately acquires no lock.
        """

        state = self._sessions.get(memory_key)
        if state is None:
            state = self._default_conversation_state()
        return ConversationSnapshot(state)

    def replace_conversation(
        self,
        memory_key: str,
        snapshot_or_state: ConversationSnapshot | Mapping[str, object],
    ) -> None:
        """Atomically replace one conversation with a fresh deep materialization.

        Replacement never shallow-merges nested state. Authenticated callers
        must hold the existing per-conversation G1C lock.
        """

        if isinstance(snapshot_or_state, ConversationSnapshot):
            state = snapshot_or_state.materialize()
        elif isinstance(snapshot_or_state, Mapping):
            state = ConversationSnapshot(snapshot_or_state).materialize()
        else:
            raise ConversationMemoryValidationError()
        if not state:
            state = self._default_conversation_state()
        self._sessions[memory_key] = state

    def install_reservation_mutation_guard(
        self,
        memory_key: str,
        *,
        status: str,
        operation: str,
    ) -> None:
        """Install a known-safe process-local guard without copying memory."""

        if (
            status not in _RESERVATION_GUARD_STATUSES
            or operation not in _RESERVATION_GUARD_OPERATIONS
        ):
            raise ReservationMutationGuardError()
        self._reservation_mutation_guards[memory_key] = {
            "status": status,
            "operation": operation,
        }

    def get_reservation_mutation_guard(
        self,
        memory_key: str,
    ) -> dict[str, str] | None:
        guard = self._reservation_mutation_guards.get(memory_key)
        return dict(guard) if guard is not None else None

    def clear_reservation_mutation_guard(self, memory_key: str) -> None:
        self._reservation_mutation_guards.pop(memory_key, None)

    def update_session(self, memory_key: str, data: dict[str, Any]) -> dict[str, Any]:
        session = self.create_session(memory_key)
        for key, value in data.items():
            if value is not None:
                session[key] = value
        return session

    def clear_session(self, memory_key: str) -> None:
        self._sessions.pop(memory_key, None)
        self._reservation_mutation_guards.pop(memory_key, None)

    def remove_session_keys(self, memory_key: str, keys) -> dict[str, Any]:
        """Remove only explicitly selected internal state from one conversation."""
        session = self.create_session(memory_key)
        for key in keys:
            session.pop(key, None)
        return session
