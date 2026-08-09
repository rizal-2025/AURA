from typing import Any

from sqlalchemy.orm import Session

from app.brain.indonesian_nlu import parse_confirmation
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_entity_extractor import (
    REFERENCE_AMBIGUITY_GUIDANCE,
    REFERENCE_DATA_UNAVAILABLE_RESPONSE,
    REFERENCE_NOT_FOUND_RESPONSE,
)
from app.brain.reservation_memory import (
    COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
    OUTCOME_UNKNOWN,
    SESSION_UNUSABLE,
    has_reservation_persistence_blocker,
    publish_cancel_success,
    publish_post_commit_memory_guard,
    publish_reservation_persistence_blocker,
    reservation_persistence_blocker_response,
)
from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.services.reservation.service import ReservationService
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
    require_canonical_public_reference,
)
from app.agents.result import ReservationOperationResult, ReservationOperationType
from app.agents.reservation_selection import (
    format_numbered_reservations,
    format_reservation_summary,
    parse_reservation_selection,
)


class CancelReservationAgent:
    """Guide a user through cancelling an existing reservation."""

    SELECT_RESERVATION_REFERENCE = "select_reservation_reference"
    CONFIRM_RESERVATION_SELECTION = "confirm_reservation_selection"
    CONFIRM_CANCELLATION = "confirm_cancellation"

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        reservation_service: ReservationService | None = None,
        workflow_state_service=None,
    ):
        self.memory_manager = memory_manager or MemoryManager()
        self.reservation_service = reservation_service or ReservationService()
        self.workflow_state_service = workflow_state_service

    async def run(
        self,
        db: Session,
        session_id: str,
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        try:
            require_owner_customer_id(owner_customer_id)
        except MissingOwnerCustomerError:
            return {
                "status": "authorization_required",
                "response": "Identitas pelanggan tidak valid atau telah kedaluwarsa.",
            }

        session = self.memory_manager.get_session(session_id)
        if has_reservation_persistence_blocker(
            self.memory_manager,
            session_id,
            session,
        ):
            return {
                "status": "persistence_uncertain",
                "response": reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    session,
                ),
            }
        stage = session.get("cancel_reservation_stage")

        if stage is None:
            return self._start_cancellation(db, session, owner_customer_id)

        if stage == self.SELECT_RESERVATION_REFERENCE:
            return self._select_reservation(db, session, user_message, owner_customer_id)

        if stage == self.CONFIRM_RESERVATION_SELECTION:
            return self._confirm_reservation_selection(
                db,
                session,
                user_message,
                owner_customer_id,
            )

        if stage == self.CONFIRM_CANCELLATION:
            return self._confirm_cancellation(
                db,
                session_id,
                session,
                user_message,
                owner_customer_id,
            )

        self._clear_cancellation_state(session)
        return self._start_cancellation(db, session, owner_customer_id)

    def _start_cancellation(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        try:
            reservations = self._list_selectable_reservations(
                db,
                owner_customer_id=owner_customer_id,
                limit=5,
            )
        except PublicReservationReferenceUnavailableError:
            self._clear_cancellation_state(session)
            return {
                "status": "reference_unavailable",
                "response": REFERENCE_DATA_UNAVAILABLE_RESPONSE,
            }
        recent_reservations = tuple(reservations[:5])

        self._clear_cancellation_state(session)
        if not recent_reservations:
            return {
                "status": "no_reservations",
                "response": "Saya tidak menemukan reservasi aktif yang dapat dibatalkan.",
            }

        candidate_references = [
            reservation.reference for reservation in recent_reservations
        ]
        session["cancel_reservation_candidate_references"] = candidate_references
        if len(recent_reservations) == 1:
            reservation = recent_reservations[0]
            session.update(
                {
                    "cancel_reservation_reference": reservation.reference,
                    "cancel_reservation_stage": self.CONFIRM_RESERVATION_SELECTION,
                }
            )
            return {
                "status": "awaiting_cancellation",
                "response": (
                    f"Saya menemukan reservasi ini:\n\n"
                    f"{format_reservation_summary(reservation)}\n\n"
                    "Apakah ini reservasi yang ingin dibatalkan? Ya / Tidak"
                ),
            }

        session["cancel_reservation_stage"] = self.SELECT_RESERVATION_REFERENCE
        return {
            "status": "awaiting_cancellation",
            "response": (
                f"Saya menemukan {len(recent_reservations)} reservasi:\n\n"
                f"{format_numbered_reservations(recent_reservations)}\n\n"
                f"Pilih reservasi: 1 sampai {len(recent_reservations)}."
            ),
        }

    def _select_reservation(
        self,
        db: Session,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        candidate_references = tuple(
            session.get("cancel_reservation_candidate_references") or ()
        )
        if len(candidate_references) < 2:
            self._clear_cancellation_state(session)
            return self._start_cancellation(db, session, owner_customer_id)

        selection = parse_reservation_selection(user_message, candidate_references)
        if selection.status == "ambiguous":
            return {
                "status": "awaiting_cancellation",
                "response": REFERENCE_AMBIGUITY_GUIDANCE,
                "invalid_input": True,
            }
        if selection.status != "valid":
            return {
                "status": "awaiting_cancellation",
                "response": (
                    f"Pilihan tidak valid. Masukkan angka 1 sampai "
                    f"{len(candidate_references)}."
                ),
                "invalid_input": True,
            }
        reservation_reference = selection.reference

        reservation = self._get_selectable_reservation_by_reference(
            db,
            reservation_reference,
            owner_customer_id=owner_customer_id,
        )
        if reservation is None:
            if reservation_reference not in candidate_references:
                return {
                    "status": "awaiting_cancellation",
                    "response": REFERENCE_NOT_FOUND_RESPONSE,
                    "invalid_input": True,
                }
            return self._restart_after_stale(db, session, owner_customer_id)

        session.update(
            {
                "cancel_reservation_reference": reservation_reference,
                "cancel_reservation_stage": self.CONFIRM_CANCELLATION,
                "cancel_reservation_candidate_references": [],
            }
        )
        return {
            "status": "awaiting_cancellation",
            "response": (
                f"Reservasi dipilih:\n\n{format_reservation_summary(reservation)}\n\n"
                "Yakin ingin membatalkan reservasi ini? Ya / Tidak"
            ),
        }

    def _confirm_reservation_selection(
        self,
        db: Session,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        confirmation = parse_confirmation(user_message)
        if confirmation == "reject":
            self._clear_cancellation_state(session)
            return {
                "status": "cancellation_rejected",
                "response": (
                    "Baik, proses pembatalan dihentikan. "
                    "Tidak ada perubahan pada reservasi."
                ),
            }
        if confirmation != "confirm":
            return {
                "status": "awaiting_cancellation",
                "response": (
                    "Mohon jawab Ya atau Tidak. Apakah ini reservasi yang "
                    "ingin dibatalkan?"
                ),
                "invalid_input": True,
            }

        reservation_reference = session.get("cancel_reservation_reference")
        reservation = self._get_selectable_reservation_by_reference(
            db,
            reservation_reference,
            owner_customer_id=owner_customer_id,
        )
        if reservation is None:
            return self._restart_after_stale(db, session, owner_customer_id)
        session.update(
            {
                "cancel_reservation_stage": self.CONFIRM_CANCELLATION,
                "cancel_reservation_candidate_references": [],
            }
        )
        return {
            "status": "awaiting_cancellation",
            "response": (
                f"Reservasi dipilih:\n\n{format_reservation_summary(reservation)}\n\n"
                "Yakin ingin membatalkan reservasi ini? Ya / Tidak"
            ),
        }

    def _confirm_cancellation(
        self,
        db: Session,
        session_id: str,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        try:
            reservation_reference = require_canonical_public_reference(
                session.get("cancel_reservation_reference")
            )
        except PublicReservationReferenceUnavailableError:
            reservation_reference = None
        if reservation_reference is None:
            self._clear_cancellation_state(session)
            return {
                "status": "awaiting_cancellation",
                "response": (
                    "Sesi pembatalan tidak valid. Mulai lagi dengan "
                    "'batalkan reservasi saya'."
                ),
            }

        confirmation = parse_confirmation(user_message)
        if confirmation == "reject":
            self._clear_cancellation_state(session)
            return {
                "status": "cancellation_rejected",
                "response": "Pembatalan reservasi dibatalkan. Tidak ada perubahan pada reservasi.",
            }

        if confirmation != "confirm":
            return {
                "status": "awaiting_cancellation",
                "response": "Yakin ingin membatalkan reservasi ini? Ya / Tidak",
            }

        current_reservation = (
            self._get_selectable_reservation_by_reference(
                db,
                reservation_reference,
                owner_customer_id=owner_customer_id,
            )
        )
        if current_reservation is None:
            return self._restart_after_stale(db, session, owner_customer_id)

        if self.workflow_state_service is not None:
            self.workflow_state_service.begin_mutation(
                db,
                owner_customer_id=owner_customer_id,
                memory_key=session_id,
                operation="cancel",
            )
        snapshot = self.memory_manager.snapshot_conversation(session_id)
        try:
            cancelled_reservation = (
                self.reservation_service.cancel_reservation_by_reference(
                    db,
                    reservation_reference,
                    owner_customer_id=owner_customer_id,
                )
            )
            if cancelled_reservation is None:
                # The cancel operation owns and fully ends the atomic mutation
                # transaction. This ownership-filtered reconciliation is a new,
                # separate read transaction used only to preserve the safe
                # already-cancelled versus unavailable response distinction.
                current_reservation = (
                    self.reservation_service.get_reservation_by_reference(
                        db,
                        reservation_reference,
                        owner_customer_id=owner_customer_id,
                    )
                )
            else:
                current_reservation = None
        except PublicReservationReferenceUnavailableError:
            self.memory_manager.replace_conversation(session_id, snapshot)
            return {
                "status": "reference_unavailable",
                "response": REFERENCE_DATA_UNAVAILABLE_RESPONSE,
            }
        except PersistenceOutcomeUnknownError:
            publish_reservation_persistence_blocker(
                self.memory_manager,
                session_id,
                snapshot,
                status=OUTCOME_UNKNOWN,
                operation="cancel",
            )
            raise
        except TransactionSessionUnusableError:
            publish_reservation_persistence_blocker(
                self.memory_manager,
                session_id,
                snapshot,
                status=SESSION_UNUSABLE,
                operation="cancel",
            )
            raise
        except PersistenceOperationError:
            self.memory_manager.replace_conversation(session_id, snapshot)
            raise

        publication_failed = False
        try:
            publish_cancel_success(
                self.memory_manager,
                session_id,
                snapshot,
            )
        except Exception:
            publication_failed = True
        if publication_failed:
            publish_post_commit_memory_guard(
                self.memory_manager,
                session_id,
                snapshot,
                operation="cancel",
            )
        if cancelled_reservation is None:
            if current_reservation is not None and self._is_cancelled(current_reservation):
                response = "Reservasi ini sudah dibatalkan. Tidak ada perubahan tambahan."
            else:
                response = REFERENCE_NOT_FOUND_RESPONSE
            return {
                "status": "awaiting_cancellation",
                "response": response,
            }

        try:
            response = (
                "Reservasi berhasil dibatalkan:\n\n"
                f"{self._format_reservation(cancelled_reservation)}"
            )
        except Exception:
            response = COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE
        return {
            "status": "cancelled",
            "response": response,
            "reservation_operation": ReservationOperationResult(
                ReservationOperationType.CANCELLED,
                reservation_reference,
            ),
        }

    def _clear_cancellation_state(self, session: dict[str, Any]) -> None:
        session["cancel_reservation_stage"] = None
        session["cancel_reservation_reference"] = None
        session["cancel_reservation_candidate_references"] = []

    def _restart_after_stale(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        refreshed = self._start_cancellation(db, session, owner_customer_id)
        refreshed["response"] = (
            "Reservasi yang dipilih tidak lagi tersedia untuk dibatalkan.\n\n"
            + refreshed["response"]
        )
        return refreshed

    def _list_selectable_reservations(
        self,
        db: Session,
        owner_customer_id,
        *,
        limit: int,
    ):
        selector = getattr(
            self.reservation_service,
            "list_selectable_reservations",
            None,
        )
        if selector is not None:
            return selector(db, owner_customer_id=owner_customer_id, limit=limit)
        reservations = self.reservation_service.list_recent_reservations(
            db,
            owner_customer_id=owner_customer_id,
            limit=limit,
        )
        return tuple(
            reservation
            for reservation in reservations
            if not self._is_cancelled(reservation)
        )

    def _get_selectable_reservation_by_reference(
        self,
        db: Session,
        reservation_reference: str,
        owner_customer_id,
    ):
        selector = getattr(
            self.reservation_service,
            "get_selectable_reservation_by_reference",
            None,
        )
        if selector is not None:
            return selector(
                db,
                reservation_reference,
                owner_customer_id=owner_customer_id,
            )
        reservation = self.reservation_service.get_reservation_by_reference(
            db,
            reservation_reference,
            owner_customer_id=owner_customer_id,
        )
        return None if reservation is None or self._is_cancelled(reservation) else reservation

    def _is_cancelled(self, reservation: Any) -> bool:
        return str(getattr(reservation, "status", "")).lower() == "cancelled"

    def _format_reservation(self, reservation: Any) -> str:
        return (
            f"Referensi reservasi: {reservation.reference}\n"
            f"Nama: {reservation.name}\n"
            f"Jumlah Orang: {reservation.people}\n"
            f"Tanggal: {reservation.date}\n"
            f"Jam: {reservation.time}\n"
            f"Status: {reservation.status}"
        )
