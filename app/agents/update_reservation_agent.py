import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.brain.indonesian_nlu import (
    normalize_indonesian_text,
    parse_confirmation,
    parse_people_count,
    parse_target_field,
)
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_entity_extractor import (
    REFERENCE_AMBIGUITY_GUIDANCE,
    REFERENCE_DATA_UNAVAILABLE_RESPONSE,
    REFERENCE_NOT_FOUND_RESPONSE,
    normalize_natural_reservation_name,
)
from app.brain.reservation_memory import (
    COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
    OUTCOME_UNKNOWN,
    SESSION_UNUSABLE,
    has_reservation_persistence_blocker,
    publish_post_commit_memory_guard,
    publish_reservation_persistence_blocker,
    publish_update_success,
    reservation_persistence_blocker_response,
)
from app.core.input_validation import (
    InputValidationError,
    validate_reservation_field,
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
from app.utils.datetime_parser import DatetimeParser


class UpdateReservationAgent:
    """Guide a user through updating an existing reservation."""

    SELECT_RESERVATION_REFERENCE = "select_reservation_reference"
    CONFIRM_RESERVATION_SELECTION = "confirm_reservation_selection"
    SELECT_FIELD = "select_field"
    INPUT_VALUE = "input_value"
    EDITABLE_FIELDS = ("name", "people", "date", "time")

    FIELD_ALIASES = {
        "name": {"name", "nama", "atas nama"},
        "people": {"people", "jumlah", "jumlah orang", "orang"},
        "date": {"date", "tanggal", "hari"},
        "time": {"time", "jam", "pukul", "waktu"},
    }

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        reservation_service: ReservationService | None = None,
        workflow_state_service=None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.memory_manager = memory_manager or MemoryManager()
        self.reservation_service = reservation_service or ReservationService()
        self.workflow_state_service = workflow_state_service
        self.clock = clock

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
        stage = session.get("update_reservation_stage")

        if stage is None:
            return self._start_update(db, session, owner_customer_id)

        if stage == self.SELECT_RESERVATION_REFERENCE:
            return self._select_reservation(db, session, user_message, owner_customer_id)

        if stage == self.CONFIRM_RESERVATION_SELECTION:
            return self._confirm_reservation_selection(
                db,
                session,
                user_message,
                owner_customer_id,
            )

        if stage == self.SELECT_FIELD:
            selection = self._select_field(session, user_message)
            selected_field = session.get("editing_field")
            normalized_message = normalize_indonesian_text(user_message)
            if (
                selected_field in self.EDITABLE_FIELDS
                and (
                    selected_field != "name"
                    or any(
                        cue in normalized_message.split()
                        for cue in ("ganti", "ubah", "jadi", "menjadi")
                    )
                )
                and self._parse_new_value(selected_field, user_message) is not None
            ):
                return self._update_field(
                    db,
                    session_id,
                    session,
                    user_message,
                    owner_customer_id,
                )
            return selection

        if stage == self.INPUT_VALUE:
            return self._update_field(
                db,
                session_id,
                session,
                user_message,
                owner_customer_id,
            )

        self._clear_update_state(session)
        return self._start_update(db, session, owner_customer_id)

    def _start_update(
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
            self._clear_update_state(session)
            return {
                "status": "reference_unavailable",
                "response": REFERENCE_DATA_UNAVAILABLE_RESPONSE,
            }
        recent_reservations = tuple(reservations[:5])

        self._clear_update_state(session)
        if not recent_reservations:
            return {
                "status": "no_reservations",
                "response": "Saya tidak menemukan reservasi aktif yang dapat diubah.",
            }

        candidate_references = [
            reservation.reference for reservation in recent_reservations
        ]
        session["update_reservation_candidate_references"] = candidate_references
        if len(recent_reservations) == 1:
            reservation = recent_reservations[0]
            session.update(
                {
                    "reservation_reference": reservation.reference,
                    "update_reservation_stage": self.CONFIRM_RESERVATION_SELECTION,
                }
            )
            return {
                "status": "awaiting_update",
                "response": (
                    f"Saya menemukan reservasi ini:\n\n"
                    f"{format_reservation_summary(reservation)}\n\n"
                    "Apakah ini reservasi yang ingin diubah? Ya / Tidak"
                ),
            }

        session["update_reservation_stage"] = self.SELECT_RESERVATION_REFERENCE
        return {
            "status": "awaiting_update",
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
            session.get("update_reservation_candidate_references") or ()
        )
        if len(candidate_references) < 2:
            self._clear_update_state(session)
            return self._start_update(db, session, owner_customer_id)

        selection = parse_reservation_selection(user_message, candidate_references)
        if selection.status == "ambiguous":
            return {
                "status": "awaiting_update",
                "response": REFERENCE_AMBIGUITY_GUIDANCE,
                "invalid_input": True,
            }
        if selection.status != "valid":
            return {
                "status": "awaiting_update",
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
                    "status": "awaiting_update",
                    "response": REFERENCE_NOT_FOUND_RESPONSE,
                    "invalid_input": True,
                }
            return self._restart_after_stale(db, session, owner_customer_id)

        session.update(
            {
                "reservation_reference": reservation_reference,
                "editing_field": None,
                "update_reservation_stage": self.SELECT_FIELD,
                "update_reservation_candidate_references": [],
            }
        )
        return {
            "status": "awaiting_update",
            "response": (
                f"Reservasi dipilih:\n\n{format_reservation_summary(reservation)}\n\n"
                "Field mana yang ingin diubah? Pilih: name, people, date, atau time."
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
            self._clear_update_state(session)
            return {
                "status": "update_rejected",
                "response": "Baik, proses perubahan reservasi dihentikan. Tidak ada perubahan.",
            }
        if confirmation != "confirm":
            return {
                "status": "awaiting_update",
                "response": "Mohon jawab Ya atau Tidak. Apakah ini reservasi yang ingin diubah?",
                "invalid_input": True,
            }

        reservation_reference = session.get("reservation_reference")
        reservation = self._get_selectable_reservation_by_reference(
            db,
            reservation_reference,
            owner_customer_id=owner_customer_id,
        )
        if reservation is None:
            return self._restart_after_stale(db, session, owner_customer_id)
        session.update(
            {
                "editing_field": None,
                "update_reservation_stage": self.SELECT_FIELD,
                "update_reservation_candidate_references": [],
            }
        )
        return {
            "status": "awaiting_update",
            "response": "Field mana yang ingin diubah? Pilih: name, people, date, atau time.",
        }

    def _select_field(self, session: dict[str, Any], user_message: str) -> dict[str, Any]:
        field_name = self._resolve_field(user_message)
        if field_name is None:
            return {
                "status": "awaiting_update",
                "response": "Field tidak valid. Pilih: name, people, date, atau time.",
                "invalid_input": True,
            }

        session.update(
            {
                "editing_field": field_name,
                "update_reservation_stage": self.INPUT_VALUE,
            }
        )
        return {
            "status": "awaiting_update",
            "response": self._question_for_field(field_name),
        }

    def _update_field(
        self,
        db: Session,
        session_id: str,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        reservation_reference = session.get("reservation_reference")
        field_name = session.get("editing_field")

        try:
            reservation_reference = require_canonical_public_reference(
                reservation_reference
            )
        except PublicReservationReferenceUnavailableError:
            reservation_reference = None
        if (
            reservation_reference is None
            or field_name not in self.EDITABLE_FIELDS
        ):
            self._clear_update_state(session)
            return {
                "status": "awaiting_update",
                "response": "Sesi update tidak valid. Mulai lagi dengan 'ubah reservasi saya'.",
            }

        snapshot = self.memory_manager.snapshot_conversation(session_id)
        new_value = self._parse_new_value(field_name, user_message)
        if new_value is None:
            return {
                "status": "awaiting_update",
                "response": self._invalid_value_response(field_name),
                "invalid_input": True,
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
                operation="update",
            )
        try:
            updated_reservation = (
                self.reservation_service.update_reservation_field_by_reference(
                    db,
                    reservation_reference,
                    field_name,
                    new_value,
                    owner_customer_id=owner_customer_id,
                )
            )
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
                operation="update",
            )
            raise
        except TransactionSessionUnusableError:
            publish_reservation_persistence_blocker(
                self.memory_manager,
                session_id,
                snapshot,
                status=SESSION_UNUSABLE,
                operation="update",
            )
            raise
        except PersistenceOperationError:
            self.memory_manager.replace_conversation(session_id, snapshot)
            raise

        if updated_reservation is None:
            publication_failed = False
            try:
                publish_update_success(
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
                    operation="update",
                )
            return {
                "status": "awaiting_update",
                "response": REFERENCE_NOT_FOUND_RESPONSE,
            }

        publication_failed = False
        try:
            publish_update_success(
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
                operation="update",
            )
        try:
            response = (
                "Reservasi berhasil diperbarui:\n\n"
                f"{self._format_reservation(updated_reservation)}"
            )
        except Exception:
            response = COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE
        return {
            "status": "updated",
            "response": response,
            "reservation_operation": ReservationOperationResult(
                ReservationOperationType.UPDATED,
                reservation_reference,
            ),
        }

    def _clear_update_state(self, session: dict[str, Any]) -> None:
        session["update_reservation_stage"] = None
        session["reservation_reference"] = None
        session["editing_field"] = None
        session["update_reservation_candidate_references"] = []

    def _restart_after_stale(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        refreshed = self._start_update(db, session, owner_customer_id)
        refreshed["response"] = (
            "Reservasi yang dipilih tidak lagi tersedia untuk diubah.\n\n"
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
            if str(getattr(reservation, "status", "")).lower() != "cancelled"
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
        if str(getattr(reservation, "status", "")).lower() == "cancelled":
            return None
        return reservation

    def _resolve_field(self, user_message: str) -> str | None:
        return parse_target_field(user_message)

    def _parse_new_value(self, field_name: str, user_message: str) -> Any:
        text = user_message
        if not text or text.isspace():
            return None

        if field_name == "name":
            for pattern in (
                r"(?:nama|namanya|atas nama)\s+(?:ganti|ubah|jadi|menjadi|ke)\s+(.+)$",
                r"(?:ganti|ubah)\s+(?:nama|namanya|atas nama)(?:\s+(?:jadi|menjadi|ke))?\s+(.+)$",
            ):
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    try:
                        return normalize_natural_reservation_name(match.group(1))
                    except InputValidationError:
                        return None
            return self._validated_field(field_name, text)

        if field_name == "people":
            return parse_people_count(text)

        if field_name == "date":
            candidate = (
                text
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text)
                else DatetimeParser.parse_date(text, clock=self.clock)
            )
            return self._validated_field(field_name, candidate)

        if field_name == "time":
            candidate = (
                text
                if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", text)
                else DatetimeParser.parse_time(text)
            )
            return self._validated_field(field_name, candidate)

        return None

    @staticmethod
    def _validated_field(field_name: str, value: Any) -> Any:
        try:
            return validate_reservation_field(field_name, value)
        except InputValidationError:
            return None

    def _question_for_field(self, field_name: str) -> str:
        questions = {
            "name": "Nama baru menjadi siapa?",
            "people": "Jumlah orang baru menjadi berapa?",
            "date": "Tanggal baru menjadi kapan?",
            "time": "Jam baru menjadi berapa?",
        }
        return questions[field_name]

    def _invalid_value_response(self, field_name: str) -> str:
        if field_name == "people":
            return (
                "Jumlah orang harus berupa angka positif. "
                "Silakan masukkan jumlah orang yang valid."
            )

        return self._clarification_for_field(field_name)

    def _clarification_for_field(self, field_name: str) -> str:
        if field_name == "date":
            return "Tanggal belum jelas. Sebutkan tanggal lengkap, misalnya 30 Juli 2026."
        if field_name == "time":
            return "Jam belum jelas. Sebutkan pagi atau malam, misalnya 07.00 atau 19.00."
        return self._question_for_field(field_name)

    def _format_reservation(self, reservation: Any) -> str:
        return (
            f"Referensi reservasi: {reservation.reference}\n"
            f"Nama: {reservation.name}\n"
            f"Jumlah Orang: {reservation.people}\n"
            f"Tanggal: {reservation.date}\n"
            f"Jam: {reservation.time}\n"
            f"Status: {reservation.status}"
        )
