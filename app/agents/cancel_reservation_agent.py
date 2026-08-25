from typing import Any

from sqlalchemy.orm import Session

from app.brain.indonesian_nlu import parse_confirmation
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    OUTCOME_UNKNOWN,
    SESSION_UNUSABLE,
    has_reservation_persistence_blocker,
    publish_cancel_success,
    publish_post_commit_memory_guard,
    publish_reservation_persistence_blocker,
    reservation_persistence_blocker_response,
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


class CancelReservationAgent:
    """Guide a user through cancelling an existing reservation."""

    SELECT_RESERVATION_REFERENCE = "select_reservation_reference"
    CONFIRM_RESERVATION_SELECTION = "confirm_reservation_selection"
    CONFIRM_CANCELLATION = "confirm_cancellation"
    PAGE_SIZE = 5

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
            page = self._list_selectable_reservation_page(
                db,
                owner_customer_id=owner_customer_id,
                after_public_reference=None,
            )
        except PublicReservationReferenceUnavailableError:
            self._clear_cancellation_state(session)
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable"),
            }
        recent_reservations = tuple(page.reservations)

        self._clear_cancellation_state(session)
        if not recent_reservations:
            return {
                "status": "no_reservations",
                "response": tr("cancel_none"),
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
                    "cancel_reservation_page_cursor": None,
                    "cancel_reservation_page_has_more": False,
                }
            )
            return {
                "status": "awaiting_cancellation",
                "response": tr(
                    "select_cancel_single",
                    summary=format_reservation_summary(reservation),
                ),
            }

        session.update(
            {
                "cancel_reservation_stage": self.SELECT_RESERVATION_REFERENCE,
                "cancel_reservation_page_cursor": None,
                "cancel_reservation_page_has_more": page.has_more,
            }
        )
        return {
            "status": "awaiting_cancellation",
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
            session.get("cancel_reservation_candidate_references") or ()
        )
        page_has_more = session.get("cancel_reservation_page_has_more")
        if not 1 <= len(candidate_references) <= self.PAGE_SIZE or type(
            page_has_more
        ) is not bool:
            self._clear_cancellation_state(session)
            return self._start_cancellation(db, session, owner_customer_id)

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
                "status": "awaiting_cancellation",
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
            if session.get("cancel_reservation_page_cursor") is not None:
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
                "status": "awaiting_cancellation",
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
                    "status": "awaiting_cancellation",
                    "response": tr("reference_not_found"),
                    "invalid_input": True,
                }
            return self._restart_after_stale(db, session, owner_customer_id)

        session.update(
            {
                "cancel_reservation_reference": reservation_reference,
                "cancel_reservation_stage": self.CONFIRM_CANCELLATION,
                "cancel_reservation_candidate_references": [],
                "cancel_reservation_page_cursor": None,
                "cancel_reservation_page_has_more": False,
            }
        )
        return {
            "status": "awaiting_cancellation",
            "response": tr(
                "cancel_selected",
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
            self._clear_cancellation_state(session)
            return {
                "status": "cancellation_rejected",
                "response": tr("cancel_selection_rejected"),
            }
        if confirmation != "confirm":
            return {
                "status": "awaiting_cancellation",
                "response": tr("cancel_yes_no_selection"),
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
                "cancel_reservation_page_cursor": None,
                "cancel_reservation_page_has_more": False,
            }
        )
        return {
            "status": "awaiting_cancellation",
            "response": tr(
                "cancel_selected",
                summary=format_reservation_summary(reservation),
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
                "response": tr("cancel_session_invalid"),
            }

        confirmation = parse_confirmation(user_message)
        if confirmation == "reject":
            self._clear_cancellation_state(session)
            return {
                "status": "cancellation_rejected",
                "response": tr("cancel_flow_stopped"),
            }

        if confirmation != "confirm":
            return {
                "status": "awaiting_cancellation",
                "response": tr("cancel_confirm"),
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
                "response": tr("reference_unavailable"),
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
                response = tr("cancel_already")
            else:
                response = tr("reference_not_found")
            return {
                "status": "awaiting_cancellation",
                "response": response,
            }

        try:
            response = tr(
                "cancel_success",
                reservation=self._format_reservation(cancelled_reservation),
            )
        except Exception:
            response = tr("committed_format_fallback")
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
        session["cancel_reservation_page_cursor"] = None
        session["cancel_reservation_page_has_more"] = None

    def _show_next_page(
        self,
        db: Session,
        session: dict[str, Any],
        candidate_references: tuple[str, ...],
        owner_customer_id,
    ) -> dict[str, Any]:
        if not session.get("cancel_reservation_page_has_more"):
            return_guidance = (
                tr("return_to_first")
                if session.get("cancel_reservation_page_cursor") is not None
                else ""
            )
            return {
                "status": "awaiting_cancellation",
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
            self._clear_cancellation_state(session)
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable"),
            }
        if not page.reservations:
            return self._restart_after_stale(db, session, owner_customer_id)
        self._store_cancellation_page(
            session,
            page.reservations,
            cursor=cursor,
            has_more=page.has_more,
        )
        return {
            "status": "awaiting_cancellation",
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
        if session.get("cancel_reservation_page_cursor") is None:
            return {
                "status": "awaiting_cancellation",
                "response": tr("already_first_page"),
                "invalid_input": True,
            }
        return self._start_cancellation(db, session, owner_customer_id)

    def _store_cancellation_page(
        self,
        session: dict[str, Any],
        reservations,
        *,
        cursor: str | None,
        has_more: bool,
    ) -> None:
        session.update(
            {
                "cancel_reservation_stage": self.SELECT_RESERVATION_REFERENCE,
                "cancel_reservation_candidate_references": [
                    reservation.reference for reservation in reservations
                ],
                "cancel_reservation_page_cursor": cursor,
                "cancel_reservation_page_has_more": has_more,
                "cancel_reservation_reference": None,
            }
        )

    def _restart_after_stale(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        refreshed = self._start_cancellation(db, session, owner_customer_id)
        refreshed["response"] = tr(
            "cancel_stale",
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
            if not self._is_cancelled(reservation)
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
        return None if reservation is None or self._is_cancelled(reservation) else reservation

    def _is_cancelled(self, reservation: Any) -> bool:
        return str(getattr(reservation, "status", "")).lower() == "cancelled"

    def _format_reservation(self, reservation: Any) -> str:
        return format_reservation(reservation)
