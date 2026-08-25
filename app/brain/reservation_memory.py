"""Reservation-owned conversation memory publication helpers."""

from __future__ import annotations

from typing import Any

from app.brain.memory_manager import ConversationSnapshot, MemoryManager
from app.core.memory_errors import (
    ConversationMemoryValidationError,
    PostCommitMemoryPublicationError,
)
from app.core.locale import tr
from app.services.reservation.dto import PersistedReservationDTO
from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
)


RESERVATION_PERSISTENCE_STATE = "reservation_persistence_state"
OUTCOME_UNKNOWN = "outcome_unknown"
SESSION_UNUSABLE = "session_unusable"
COMMITTED_MEMORY_UNAVAILABLE = "committed_memory_unavailable"
MUTATION_RECONCILIATION_REQUIRED = "mutation_reconciliation_required"
RESERVATION_OPERATIONS = frozenset({"create", "update", "cancel"})
RESERVATION_PERSISTENCE_STATUSES = frozenset(
    {
        OUTCOME_UNKNOWN,
        SESSION_UNUSABLE,
        COMMITTED_MEMORY_UNAVAILABLE,
        MUTATION_RECONCILIATION_REQUIRED,
    }
)
RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE = (
    "Maaf, perubahan belum dapat dipastikan. "
    "Silakan periksa status lalu coba lagi."
)
COMMITTED_OPERATION_STATE_UNAVAILABLE_RESPONSE = (
    "Proses telah selesai, tetapi status percakapan tidak dapat diperbarui. "
    "Silakan cek daftar reservasi sebelum mencoba lagi."
)
COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE = (
    "Proses reservasi telah selesai, tetapi konfirmasi rinci tidak dapat "
    "ditampilkan. Silakan lihat daftar reservasi Anda untuk memverifikasi status."
)


def get_reservation_persistence_blocker(
    memory_manager: MemoryManager,
    memory_key: str,
    state: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    emergency_guard = memory_manager.get_reservation_mutation_guard(memory_key)
    if emergency_guard is not None:
        return emergency_guard
    if state is None:
        state = memory_manager.get_session(memory_key)
    blocker = state.get(RESERVATION_PERSISTENCE_STATE)
    if (
        isinstance(blocker, dict)
        and set(blocker) == {"status", "operation"}
        and blocker.get("status") in RESERVATION_PERSISTENCE_STATUSES
        and blocker.get("operation") in RESERVATION_OPERATIONS
    ):
        return {
            "status": blocker["status"],
            "operation": blocker["operation"],
        }
    return None


def has_reservation_persistence_blocker(
    memory_manager: MemoryManager,
    memory_key: str,
    state: dict[str, Any] | None = None,
) -> bool:
    blocker = get_reservation_persistence_blocker(
        memory_manager,
        memory_key,
        state,
    )
    return blocker is not None


def reservation_persistence_blocker_response(
    memory_manager: MemoryManager,
    memory_key: str,
    state: dict[str, Any] | None = None,
) -> str:
    blocker = get_reservation_persistence_blocker(
        memory_manager,
        memory_key,
        state,
    )
    return (
        tr("committed_state_unavailable")
        if blocker is not None
        and blocker.get("status") == COMMITTED_MEMORY_UNAVAILABLE
        else tr("persistence_uncertain")
    )


def publish_create_success(
    memory_manager: MemoryManager,
    memory_key: str,
    snapshot: ConversationSnapshot,
    persisted_reservation: PersistedReservationDTO,
) -> None:
    if type(persisted_reservation) is not PersistedReservationDTO:
        raise ConversationMemoryValidationError()
    try:
        reference = canonicalize_public_reference(
            persisted_reservation.reference
        )
    except InvalidPublicReservationReferenceError:
        raise ConversationMemoryValidationError() from None
    if reference != persisted_reservation.reference:
        raise ConversationMemoryValidationError()
    state = snapshot.materialize()
    state.update(
        {
            "name": persisted_reservation.name,
            "people": persisted_reservation.people,
            "date": persisted_reservation.date,
            "time": persisted_reservation.time,
            "completed": True,
            "awaiting_confirmation": False,
            "editing_field": None,
            "reservation_reference": reference,
        }
    )
    state.pop(RESERVATION_PERSISTENCE_STATE, None)
    memory_manager.replace_conversation(memory_key, state)
    memory_manager.clear_reservation_mutation_guard(memory_key)


def publish_update_success(
    memory_manager: MemoryManager,
    memory_key: str,
    snapshot: ConversationSnapshot,
) -> None:
    state = snapshot.materialize()
    state["update_reservation_stage"] = None
    state["update_reservation_candidate_references"] = []
    state["update_reservation_page_cursor"] = None
    state["update_reservation_page_has_more"] = None
    state["reservation_reference"] = None
    state["editing_field"] = None
    state.pop(RESERVATION_PERSISTENCE_STATE, None)
    memory_manager.replace_conversation(memory_key, state)
    memory_manager.clear_reservation_mutation_guard(memory_key)


def publish_cancel_success(
    memory_manager: MemoryManager,
    memory_key: str,
    snapshot: ConversationSnapshot,
) -> None:
    state = snapshot.materialize()
    state["cancel_reservation_stage"] = None
    state["cancel_reservation_candidate_references"] = []
    state["cancel_reservation_page_cursor"] = None
    state["cancel_reservation_page_has_more"] = None
    state["cancel_reservation_reference"] = None
    state.pop(RESERVATION_PERSISTENCE_STATE, None)
    memory_manager.replace_conversation(memory_key, state)
    memory_manager.clear_reservation_mutation_guard(memory_key)


def publish_reservation_persistence_blocker(
    memory_manager: MemoryManager,
    memory_key: str,
    snapshot: ConversationSnapshot,
    *,
    status: str,
    operation: str,
) -> None:
    if status not in RESERVATION_PERSISTENCE_STATUSES:
        raise ValueError("invalid reservation persistence status")
    if operation not in RESERVATION_OPERATIONS:
        raise ValueError("invalid reservation persistence operation")

    memory_manager.install_reservation_mutation_guard(
        memory_key,
        status=status,
        operation=operation,
    )
    state = snapshot.materialize()
    if operation == "create":
        state["awaiting_confirmation"] = False
        state["editing_field"] = None
    elif operation == "update":
        state["update_reservation_stage"] = None
        state["update_reservation_candidate_references"] = []
        state["update_reservation_page_cursor"] = None
        state["update_reservation_page_has_more"] = None
        state["reservation_reference"] = None
        state["editing_field"] = None
    else:
        state["cancel_reservation_stage"] = None
        state["cancel_reservation_candidate_references"] = []
        state["cancel_reservation_page_cursor"] = None
        state["cancel_reservation_page_has_more"] = None
        state["cancel_reservation_reference"] = None

    state[RESERVATION_PERSISTENCE_STATE] = {
        "status": status,
        "operation": operation,
    }
    memory_manager.replace_conversation(memory_key, state)


def publish_post_commit_memory_guard(
    memory_manager: MemoryManager,
    memory_key: str,
    snapshot: ConversationSnapshot,
    *,
    operation: str,
) -> None:
    """Fail closed after a confirmed commit, then raise a stable safe error."""

    guard_publication_failed = False
    try:
        publish_reservation_persistence_blocker(
            memory_manager,
            memory_key,
            snapshot,
            status=COMMITTED_MEMORY_UNAVAILABLE,
            operation=operation,
        )
    except Exception:
        guard_publication_failed = True
    if guard_publication_failed:
        fallback_guard_failed = False
        try:
            memory_manager.install_reservation_mutation_guard(
                memory_key,
                status=COMMITTED_MEMORY_UNAVAILABLE,
                operation=operation,
            )
        except Exception:
            fallback_guard_failed = True
        if fallback_guard_failed:
            raise PostCommitMemoryPublicationError() from None
    raise PostCommitMemoryPublicationError() from None
