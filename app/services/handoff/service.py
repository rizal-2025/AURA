from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.brain.memory_manager import MemoryManager


@dataclass(frozen=True)
class HandoffState:
    handoff_required: bool
    category: str
    reason_code: str
    priority: str
    safe_summary: str
    created_at: str
    attempt_count: int


class HandoffService:
    """Keep safe handoff state in the existing customer-scoped memory store."""

    STATE_KEY = "handoff_state"
    REQUIRED_KEY = "handoff_required"
    CATEGORIES = {
        "explicit_human_request",
        "repeated_misunderstanding",
        "repeated_invalid_input",
        "customer_frustration",
        "internal_error",
        "ambiguous_intent",
    }
    SAFE_SUMMARIES = {
        "explicit_human_request": "Customer requested human assistance.",
        "repeated_misunderstanding": "Automated intent understanding failed repeatedly.",
        "repeated_invalid_input": "The active workflow received repeated invalid input.",
        "customer_frustration": "Customer reported a poor automated assistance experience.",
        "internal_error": "An internal service error prevented safe completion.",
        "ambiguous_intent": "The requested reservation action remained ambiguous.",
    }

    def __init__(self, memory_manager: MemoryManager):
        self.memory_manager = memory_manager

    def is_required(self, memory_key: str) -> bool:
        return bool(self.memory_manager.get_session(memory_key).get(self.REQUIRED_KEY))

    def get_state(self, memory_key: str) -> dict[str, Any] | None:
        state = self.memory_manager.get_session(memory_key).get(self.STATE_KEY)
        return dict(state) if isinstance(state, dict) else None

    def require_handoff(self, memory_key: str, category: str, attempt_count: int = 1) -> HandoffState:
        if category not in self.CATEGORIES:
            raise ValueError("Unsupported handoff category.")
        state = HandoffState(
            handoff_required=True,
            category=category,
            reason_code=category,
            priority="high" if category in {"explicit_human_request", "internal_error", "customer_frustration"} else "normal",
            safe_summary=self.SAFE_SUMMARIES[category],
            created_at=datetime.now(timezone.utc).isoformat(),
            attempt_count=max(1, attempt_count),
        )
        self.memory_manager.update_session(
            memory_key,
            {self.REQUIRED_KEY: True, self.STATE_KEY: asdict(state)},
        )
        return state

    def clear_for_test(self, memory_key: str) -> None:
        """Internal-only reset hook; deliberately not exposed through an API."""
        session = self.memory_manager.get_session(memory_key)
        session[self.REQUIRED_KEY] = False
        session[self.STATE_KEY] = None
        session["misunderstanding_count"] = 0
        session["ambiguity_count"] = 0
        session["invalid_input_context"] = None
        session["invalid_input_count"] = 0

    def record_misunderstanding(self, memory_key: str) -> int:
        session = self.memory_manager.get_session(memory_key)
        count = int(session.get("misunderstanding_count") or 0) + 1
        session["misunderstanding_count"] = count
        return count

    def reset_misunderstandings(self, memory_key: str) -> None:
        self.memory_manager.get_session(memory_key)["misunderstanding_count"] = 0

    def record_ambiguity(self, memory_key: str) -> int:
        session = self.memory_manager.get_session(memory_key)
        count = int(session.get("ambiguity_count") or 0) + 1
        session["ambiguity_count"] = count
        return count

    def reset_ambiguity(self, memory_key: str) -> None:
        self.memory_manager.get_session(memory_key)["ambiguity_count"] = 0

    def record_invalid_input(self, memory_key: str, workflow: str, stage: str | None) -> int:
        session = self.memory_manager.get_session(memory_key)
        context = f"{workflow}:{stage or 'unknown'}"
        if session.get("invalid_input_context") != context:
            session["invalid_input_context"] = context
            session["invalid_input_count"] = 0
        session["invalid_input_count"] = int(session.get("invalid_input_count") or 0) + 1
        return session["invalid_input_count"]

    def reset_invalid_input(self, memory_key: str) -> None:
        session = self.memory_manager.get_session(memory_key)
        session["invalid_input_context"] = None
        session["invalid_input_count"] = 0

    @staticmethod
    def explicit_response() -> str:
        return "Baik, saya akan meneruskan percakapan ini kepada petugas."

    @staticmethod
    def required_response() -> str:
        return (
            "Maaf, saya belum berhasil menyelesaikan kendala Anda. Percakapan ini perlu "
            "diteruskan kepada petugas agar dapat ditangani dengan lebih baik."
        )

    @staticmethod
    def waiting_response() -> str:
        return "Percakapan ini sedang menunggu bantuan petugas."

    def status_response(self, memory_key: str) -> str:
        state = self.get_state(memory_key)
        if state is None:
            return "Tidak ada permintaan bantuan petugas yang aktif."
        return "Percakapan ini sedang menunggu bantuan petugas."
