from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.brain.memory_manager import MemoryManager
from app.core.ownership import require_owner_customer_id
from app.db.models.support_ticket import (
    ACTIVE_TICKET_STATUSES,
    SAFE_TICKET_SUMMARIES,
    VALID_TICKET_PRIORITIES,
    safe_summary_for,
)
from app.services.handoff.ticket_service import TicketService


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
    SAFE_SUMMARIES = SAFE_TICKET_SUMMARIES

    def __init__(self, memory_manager: MemoryManager, ticket_service=None):
        self.memory_manager = memory_manager
        self.ticket_service = ticket_service or TicketService()

    def is_required(self, memory_key: str) -> bool:
        return bool(self.memory_manager.get_session(memory_key).get(self.REQUIRED_KEY))

    def get_state(self, memory_key: str) -> dict[str, Any] | None:
        state = self.memory_manager.get_session(memory_key).get(self.STATE_KEY)
        return dict(state) if isinstance(state, dict) else None

    def require_handoff(self, memory_key: str, category: str, attempt_count: int = 1, db=None, owner_customer_id=None) -> HandoffState:
        require_owner_customer_id(owner_customer_id)
        if category not in self.CATEGORIES:
            raise ValueError("Unsupported handoff category.")
        state = HandoffState(
            handoff_required=True,
            category=category,
            reason_code=category,
            priority="high" if category in {"explicit_human_request", "internal_error", "customer_frustration"} else "medium",
            safe_summary=self.SAFE_SUMMARIES[category],
            created_at=datetime.now(timezone.utc).isoformat(),
            attempt_count=max(1, attempt_count),
        )
        state_data = asdict(state)
        self.memory_manager.update_session(
            memory_key,
            {self.REQUIRED_KEY: True, self.STATE_KEY: state_data},
        )
        if db is not None and owner_customer_id is not None:
            try:
                ticket = self.ticket_service.create_or_get(
                    db,
                    owner_customer_id=owner_customer_id,
                    memory_key=memory_key,
                    handoff_state=state_data,
                )
                state_data["ticket_id"] = ticket.id
                state_data["ticket_number"] = ticket.ticket_number
            except Exception:
                # Automation remains locked even if PostgreSQL is unavailable.
                state_data["ticket_creation_failed"] = True
        return state

    def restore_active_handoff(self, memory_key: str, db, owner_customer_id):
        """Reconcile memory with the trusted active ticket for this owner/session."""
        require_owner_customer_id(owner_customer_id)
        had_memory_lock = self.is_required(memory_key)
        ticket = self.ticket_service.get_active(
            db,
            owner_customer_id=owner_customer_id,
            memory_key=memory_key,
        )
        if ticket is None:
            if had_memory_lock:
                state = self.get_state(memory_key) or {}
                # A ticket persistence failure has no trusted terminal state.
                # Keep the existing fail-safe automation lock; only a formerly
                # persisted ticket that is no longer active may release it.
                if state.get("ticket_creation_failed"):
                    return state
                self.clear_handoff_state(memory_key)
            return None
        if ticket.status not in ACTIVE_TICKET_STATUSES:
            return None
        if ticket.priority not in VALID_TICKET_PRIORITIES:
            raise ValueError("Invalid persisted ticket priority.")

        safe_summary = safe_summary_for(
            category=ticket.category,
            reason_code=ticket.reason_code,
        )
        created_at = (
            ticket.created_at.isoformat()
            if hasattr(ticket.created_at, "isoformat")
            else str(ticket.created_at)
        )
        state_data = {
            self.REQUIRED_KEY: True,
            "category": ticket.category,
            "reason_code": ticket.reason_code,
            "priority": ticket.priority,
            "safe_summary": safe_summary,
            "status": ticket.status,
            "created_at": created_at,
            "attempt_count": max(1, int(ticket.attempt_count)),
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
        }
        self.memory_manager.update_session(
            memory_key,
            {self.REQUIRED_KEY: True, self.STATE_KEY: state_data},
        )
        return dict(state_data)

    def clear_handoff_state(self, memory_key: str) -> None:
        """Clear only handoff-related state while preserving workflow memory."""
        self.memory_manager.remove_session_keys(
            memory_key,
            {
                self.REQUIRED_KEY,
                self.STATE_KEY,
                "misunderstanding_count",
                "ambiguity_count",
                "invalid_input_context",
                "invalid_input_count",
            },
        )

    def clear_for_test(self, memory_key: str) -> None:
        """Internal-only reset hook; deliberately not exposed through an API."""
        self.clear_handoff_state(memory_key)
        session = self.memory_manager.get_session(memory_key)
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

    def explicit_response(self, memory_key: str | None = None) -> str:
        return "Baik, saya akan meneruskan percakapan ini kepada petugas." + self._ticket_suffix(memory_key)

    def required_response(self, memory_key: str | None = None) -> str:
        return (
            "Maaf, saya belum berhasil menyelesaikan kendala Anda. Percakapan ini perlu "
            "diteruskan kepada petugas agar dapat ditangani dengan lebih baik."
        ) + self._ticket_suffix(memory_key)

    def waiting_response(self, memory_key: str | None = None) -> str:
        return "Percakapan ini sedang menunggu bantuan petugas." + self._ticket_suffix(memory_key)

    @staticmethod
    def recovery_error_response() -> str:
        return "Maaf, status bantuan petugas belum dapat diperiksa. Silakan coba lagi."

    def status_response(self, memory_key: str) -> str:
        state = self.get_state(memory_key)
        if state is None:
            return "Tidak ada permintaan bantuan petugas yang aktif."
        return self.waiting_response(memory_key)

    def _ticket_suffix(self, memory_key: str | None) -> str:
        if memory_key is None:
            return ""
        state = self.get_state(memory_key) or {}
        number = state.get("ticket_number")
        return f"\nNomor tiket Anda: {number}" if number else ""
