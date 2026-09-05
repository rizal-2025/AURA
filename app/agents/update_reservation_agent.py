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
    normalize_natural_reservation_name,
)
from app.brain.reservation_memory import (
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
from app.core.locale import SupportedLocale, current_locale, format_reservation, tr
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.services.reservation.service import ReservationService
from app.services.reservation.dto import ReservationSelectionPage
from app.services.reservation.errors import (
    PastReservationDateError,
    PastReservationTimeError,
)
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
    require_canonical_public_reference,
)
from app.agents.result import ReservationOperationResult, ReservationOperationType
from app.agents.reservation_selection import (
    format_paginated_selection,
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
    PAGE_SIZE = 5
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
        self.reservation_service = reservation_service or ReservationService(
            clock=clock
        )
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
                "response": tr("authorization_required"),
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
            page = self._list_selectable_reservation_page(
                db,
                owner_customer_id=owner_customer_id,
                after_public_reference=None,
            )
        except PublicReservationReferenceUnavailableError:
            self._clear_update_state(session)
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable"),
            }
        recent_reservations = tuple(page.reservations)

        self._clear_update_state(session)
        if not recent_reservations:
            return {
                "status": "no_reservations",
                "response": tr("update_none"),
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
                    "update_reservation_page_cursor": None,
                    "update_reservation_page_has_more": False,
                }
            )
            return {
                "status": "awaiting_update",
                "response": tr(
                    "select_update_single",
                    summary=format_reservation_summary(reservation),
                ),
            }

        session.update(
            {
                "update_reservation_stage": self.SELECT_RESERVATION_REFERENCE,
                "update_reservation_page_cursor": None,
                "update_reservation_page_has_more": page.has_more,
            }
        )
        return {
            "status": "awaiting_update",
            "response": format_paginated_selection(
                recent_reservations,
                has_more=page.has_more,
                is_later_page=False,
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
        page_has_more = session.get("update_reservation_page_has_more")
        if not 1 <= len(candidate_references) <= self.PAGE_SIZE or type(
            page_has_more
        ) is not bool:
            self._clear_update_state(session)
            return self._start_update(db, session, owner_customer_id)

        selection = parse_reservation_selection(user_message, candidate_references)
        if selection.status == "next_page":
            return self._show_next_page(
                db,
                session,
                candidate_references,
                owner_customer_id,
            )
        if selection.status == "first_page":
            return self._show_first_page(
                db,
                session,
                owner_customer_id,
            )
        if selection.status == "ambiguous":
            return {
                "status": "awaiting_update",
                "response": tr("reference_ambiguous"),
                "invalid_input": True,
            }
        if selection.status != "valid":
            navigation = []
            if page_has_more:
                navigation.append(
                    '"next"'
                    if current_locale() is SupportedLocale.EN_US
                    else '"berikutnya"'
                )
            if session.get("update_reservation_page_cursor") is not None:
                navigation.append(
                    '"first"'
                    if current_locale() is SupportedLocale.EN_US
                    else '"awal"'
                )
            navigation_guidance = (
                tr("selection_navigation", commands=" / ".join(navigation))
                if navigation
                else ""
            )
            return {
                "status": "awaiting_update",
                "response": tr(
                    "invalid_selection",
                    count=len(candidate_references),
                    guidance=navigation_guidance,
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
                    "response": tr("reference_not_found"),
                    "invalid_input": True,
                }
            return self._restart_after_stale(db, session, owner_customer_id)

        session.update(
            {
                "reservation_reference": reservation_reference,
                "editing_field": None,
                "update_reservation_stage": self.SELECT_FIELD,
                "update_reservation_candidate_references": [],
                "update_reservation_page_cursor": None,
                "update_reservation_page_has_more": False,
            }
        )
        return {
            "status": "awaiting_update",
            "response": tr(
                "selected_update",
                summary=format_reservation_summary(reservation),
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
                "response": tr("update_stopped"),
            }
        if confirmation != "confirm":
            return {
                "status": "awaiting_update",
                "response": tr("update_yes_no"),
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
                "update_reservation_page_cursor": None,
                "update_reservation_page_has_more": False,
            }
        )
        return {
            "status": "awaiting_update",
            "response": tr("choose_update_field"),
        }

    def _select_field(self, session: dict[str, Any], user_message: str) -> dict[str, Any]:
        field_name = self._resolve_field(user_message)
        if field_name is None:
            return {
                "status": "awaiting_update",
                "response": tr("invalid_update_field"),
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
                "response": tr("update_session_invalid"),
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

        update_kwargs = {"owner_customer_id": owner_customer_id}
        if self.workflow_state_service is not None:
            def mark_mutation() -> None:
                self.workflow_state_service.begin_mutation(
                    db,
                    owner_customer_id=owner_customer_id,
                    memory_key=session_id,
                    operation="update",
                )

            update_kwargs["before_mutation"] = mark_mutation
        try:
            updated_reservation = (
                self.reservation_service.update_reservation_field_by_reference(
                    db,
                    reservation_reference,
                    field_name,
                    new_value,
                    **update_kwargs,
                )
            )
        except PastReservationDateError:
            return {
                "status": "awaiting_update",
                "response": tr("past_reservation_date"),
                "invalid_input": True,
            }
        except PastReservationTimeError:
            return {
                "status": "awaiting_update",
                "response": tr("past_reservation_time"),
                "invalid_input": True,
            }
        except PublicReservationReferenceUnavailableError:
            self.memory_manager.replace_conversation(session_id, snapshot)
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable"),
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
                "response": tr("reference_not_found"),
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
            response = tr(
                "update_success",
                reservation=self._format_reservation(updated_reservation),
            )
        except Exception:
            response = tr("committed_format_fallback")
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
        session["update_reservation_page_cursor"] = None
        session["update_reservation_page_has_more"] = None

    def _show_next_page(
        self,
        db: Session,
        session: dict[str, Any],
        candidate_references: tuple[str, ...],
        owner_customer_id,
    ) -> dict[str, Any]:
        if not session.get("update_reservation_page_has_more"):
            return_guidance = (
                tr("return_to_first")
                if session.get("update_reservation_page_cursor") is not None
                else ""
            )
            return {
                "status": "awaiting_update",
                "response": tr("no_next_page", guidance=return_guidance),
                "invalid_input": True,
            }
        cursor = candidate_references[-1]
        try:
            page = self._list_selectable_reservation_page(
                db,
                owner_customer_id=owner_customer_id,
                after_public_reference=cursor,
            )
        except PublicReservationReferenceUnavailableError:
            self._clear_update_state(session)
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable"),
            }
        if not page.reservations:
            return self._restart_after_stale(db, session, owner_customer_id)
        self._store_update_page(
            session,
            page.reservations,
            cursor=cursor,
            has_more=page.has_more,
        )
        return {
            "status": "awaiting_update",
            "response": format_paginated_selection(
                page.reservations,
                has_more=page.has_more,
                is_later_page=True,
            ),
        }

    def _show_first_page(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        if session.get("update_reservation_page_cursor") is None:
            return {
                "status": "awaiting_update",
                "response": tr("already_first_page"),
                "invalid_input": True,
            }
        return self._start_update(db, session, owner_customer_id)

    def _store_update_page(
        self,
        session: dict[str, Any],
        reservations,
        *,
        cursor: str | None,
        has_more: bool,
    ) -> None:
        session.update(
            {
                "update_reservation_stage": self.SELECT_RESERVATION_REFERENCE,
                "update_reservation_candidate_references": [
                    reservation.reference for reservation in reservations
                ],
                "update_reservation_page_cursor": cursor,
                "update_reservation_page_has_more": has_more,
                "reservation_reference": None,
                "editing_field": None,
            }
        )

    def _restart_after_stale(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        refreshed = self._start_update(db, session, owner_customer_id)
        refreshed["response"] = tr(
            "update_stale",
            selection=refreshed["response"],
        )
        return refreshed

    def _list_selectable_reservation_page(
        self,
        db: Session,
        owner_customer_id,
        *,
        after_public_reference: str | None,
    ):
        page_selector = getattr(
            self.reservation_service,
            "list_selectable_reservation_page",
            None,
        )
        if page_selector is not None:
            return page_selector(
                db,
                owner_customer_id=owner_customer_id,
                after_public_reference=after_public_reference,
                page_size=self.PAGE_SIZE,
            )
        selector = getattr(
            self.reservation_service,
            "list_selectable_reservations",
            None,
        )
        if selector is not None and after_public_reference is None:
            reservations = selector(
                db,
                owner_customer_id=owner_customer_id,
                limit=self.PAGE_SIZE,
            )
            return ReservationSelectionPage(
                reservations=tuple(reservations[: self.PAGE_SIZE]),
                has_more=False,
            )
        if after_public_reference is not None:
            return ReservationSelectionPage(reservations=(), has_more=False)
        reservations = self.reservation_service.list_recent_reservations(
            db,
            owner_customer_id=owner_customer_id,
            limit=self.PAGE_SIZE,
        )
        filtered = tuple(
            reservation
            for reservation in reservations
            if str(getattr(reservation, "status", "")).lower() != "cancelled"
        )
        return ReservationSelectionPage(reservations=filtered, has_more=False)

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
            "name": "ask_new_name",
            "people": "ask_new_people",
            "date": "ask_new_date",
            "time": "ask_new_time",
        }
        return tr(questions[field_name])

    def _invalid_value_response(self, field_name: str) -> str:
        if field_name == "people":
            return tr("invalid_people")

        return self._clarification_for_field(field_name)

    def _clarification_for_field(self, field_name: str) -> str:
        if field_name == "date":
            return tr("unclear_date")
        if field_name == "time":
            return tr("unclear_time")
        return self._question_for_field(field_name)

    def _format_reservation(self, reservation: Any) -> str:
        return format_reservation(reservation)
