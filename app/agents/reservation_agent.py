import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.brain.context_resolver import ContextResolver
from app.brain.conversation_state_manager import ConversationStateManager
from app.brain.indonesian_nlu import (
    normalize_indonesian_text,
    parse_confirmation,
    parse_people_count,
    parse_target_field,
)
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    OUTCOME_UNKNOWN,
    SESSION_UNUSABLE,
    has_reservation_persistence_blocker,
    publish_create_success,
    publish_post_commit_memory_guard,
    publish_reservation_persistence_blocker,
    reservation_persistence_blocker_response,
)
from app.brain.reservation_entity_extractor import (
    ReservationEntityExtractor,
    normalize_natural_reservation_name,
)
from app.agents.result import ReservationOperationResult, ReservationOperationType
from app.core.input_validation import (
    InputValidationError,
    validate_reservation_field,
)
from app.core.locale import format_date, format_time, tr
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.memory.long_term_memory import LongTermMemoryManager
from app.schemas.reservation import ReservationCreate
from app.services.reservation.errors import (
    PastReservationDateError,
    PastReservationTimeError,
)
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
    require_canonical_public_reference,
)
from app.services.reservation.service import ReservationService
from app.utils.datetime_parser import DatetimeParser
from app.utils.reservation_date_input import PENDING_DAY, continue_date, inferred_year


CONFIRM = "CONFIRM"
REJECT = "REJECT"
EDIT_FIELD = "EDIT_FIELD"


class ReservationAgent:
    """Handle reservation-related workflow steps."""

    EDITABLE_FIELDS = ("name", "people", "date", "time")
    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        workflow_state_service=None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.memory_manager = memory_manager or MemoryManager()
        self.workflow_state_service = workflow_state_service
        self.clock = clock
        self.entity_extractor = ReservationEntityExtractor(clock=clock)
        self.reservation_service = ReservationService(clock=clock)
        self.conversation_state_manager = ConversationStateManager()
        self.context_resolver = ContextResolver()
        self.long_term_memory = LongTermMemoryManager()

    async def run(
        self,
        steps: list[dict[str, Any]],
        session_state: dict[str, Any],
        user_message: str,
        session_id: str | None = None,
        owner_customer_id=None,
        db=None,
    ) -> dict[str, Any]:
        current_session_id = session_id or str(session_state.get("session_id") or "default")
        current_state = self.memory_manager.get_session(current_session_id)
        if has_reservation_persistence_blocker(
            self.memory_manager,
            current_session_id,
            current_state,
        ):
            return {
                "status": "persistence_uncertain",
                "response": reservation_persistence_blocker_response(
                    self.memory_manager,
                    current_session_id,
                    current_state,
                ),
            }
        if session_state.get("awaiting_confirmation"):
            return await self.handle_confirmation(
                user_message,
                current_session_id,
                owner_customer_id=owner_customer_id,
                db=db,
            )

        step = steps[0] if steps else None
        if step is None:
            return {
                "status": "completed",
                "response": tr("no_available_step"),
            }

        if not session_state.get("date"):
            user_message = continue_date(user_message, session_state, clock=self.clock)
            if PENDING_DAY in session_state:
                self.memory_manager.update_session(current_session_id, {PENDING_DAY: session_state[PENDING_DAY]})
            else:
                self.memory_manager.remove_session_keys(current_session_id, (PENDING_DAY,))
        extracted = await self.entity_extractor.extract(user_message)
        pending_field = self._infer_pending_field(session_state)
        candidates = dict(extracted or {})
        pending_value_invalid = False
        if pending_field and not candidates.get(pending_field):
            inferred_value = self._infer_value_for_field(pending_field, user_message)
            if inferred_value is not None:
                candidates[pending_field] = inferred_value
            else:
                pending_value_invalid = True

        updates = {}
        invalid_fields = set()
        inferred_create_year = inferred_year(user_message, clock=self.clock)
        past_date_invalid = False
        past_time_invalid = False
        for key, value in candidates.items():
            if key not in self.EDITABLE_FIELDS or value is None:
                continue
            normalized_value = self._normalize_and_validate_field(key, value)
            if normalized_value is None:
                invalid_fields.add(key)
                continue
            if key == "date":
                if inferred_create_year is not None:
                    invalid_fields.add(key)
                    continue
                try:
                    self.reservation_service.validate_new_reservation_date(
                        normalized_value
                    )
                except PastReservationDateError:
                    invalid_fields.add(key)
                    past_date_invalid = True
                    continue
            if key == "time":
                reservation_date = updates.get("date") or session_state.get(
                    "date"
                )
                if reservation_date is not None:
                    try:
                        self.reservation_service.validate_new_reservation_datetime(
                            reservation_date,
                            normalized_value,
                        )
                    except PastReservationDateError:
                        invalid_fields.add("date")
                        past_date_invalid = True
                        continue
                    except PastReservationTimeError:
                        invalid_fields.add(key)
                        past_time_invalid = True
                        continue
            updates[key] = normalized_value
        if updates:
            session_state = dict(session_state)
            resolved_state = self.context_resolver.resolve(session_state, user_message, updates)
            self.memory_manager.update_session(current_session_id, resolved_state)
            session_state = resolved_state

        if session_state.get("user_id"):
            preferences = self.long_term_memory.suggest_context(session_state["user_id"])
            if preferences.get("favorite_name") and not session_state.get("name"):
                preferred_name = self._normalize_and_validate_field(
                    "name",
                    preferences["favorite_name"],
                )
                if preferred_name is not None:
                    session_state["name"] = preferred_name
            if preferences.get("preferred_people") and not session_state.get("people"):
                preferred_people = self._normalize_and_validate_field(
                    "people",
                    preferences["preferred_people"],
                )
                if preferred_people is not None:
                    session_state["people"] = preferred_people
            if preferences.get("favorite_time") and not session_state.get("time"):
                favorite_time = self._normalize_and_validate_field(
                    "time",
                    preferences["favorite_time"],
                )
                if favorite_time is not None:
                    session_state["time"] = favorite_time

            profile_updates = {}
            if session_state.get("name"):
                profile_updates["favorite_name"] = session_state["name"]
            if session_state.get("people"):
                profile_updates["preferred_people"] = session_state["people"]
            if session_state.get("time"):
                profile_updates["favorite_time"] = session_state["time"]
            if session_state.get("table"):
                profile_updates["favorite_table"] = session_state["table"]
            if profile_updates:
                self.long_term_memory.merge_preferences(session_state["user_id"], profile_updates)

        action = step.get("action")
        if action == "collect_missing_fields":
            if inferred_create_year is not None:
                return self._request_explicit_create_year(
                    current_session_id, inferred_create_year,
                )
            # A newly supplied date can make an already collected time past.
            # Validate the pair before presenting it as ready to confirm.
            if session_state.get("date") and session_state.get("time"):
                try:
                    self.reservation_service.validate_new_reservation_datetime(
                        session_state["date"], session_state["time"],
                    )
                except PastReservationDateError:
                    past_date_invalid = True
                except PastReservationTimeError:
                    past_time_invalid = True
            if past_date_invalid:
                self.memory_manager.remove_session_keys(
                    current_session_id,
                    ("date",),
                )
                self.memory_manager.update_session(
                    current_session_id,
                    {
                        "date": None,
                        "awaiting_confirmation": False,
                        "editing_field": None,
                    },
                )
                return {
                    "status": "awaiting_input",
                    "response": tr("past_reservation_date"),
                    "field": "date",
                    "next_action": "ask_date",
                    "invalid_input": True,
                }
            if past_time_invalid:
                self.memory_manager.remove_session_keys(
                    current_session_id,
                    ("time",),
                )
                self.memory_manager.update_session(
                    current_session_id,
                    {
                        "time": None,
                        "awaiting_confirmation": False,
                        "editing_field": None,
                        "asked_fields": list(self.conversation_state_manager.REQUIRED_FIELDS),
                    },
                )
                return {
                    "status": "awaiting_input",
                    "response": tr("past_reservation_time"),
                    "field": "time",
                    "next_action": "ask_time",
                    "invalid_input": True,
                }
            if pending_field and (
                pending_value_invalid or pending_field in invalid_fields
            ):
                return {
                    "status": "awaiting_input",
                    "response": self._clarification_for_field(
                        pending_field,
                        user_message,
                    ),
                    "field": pending_field,
                    "next_action": f"ask_{pending_field}",
                    "invalid_input": True,
                }
            reasoning_state = dict(session_state)
            next_action = self.conversation_state_manager.get_next_action(reasoning_state)
            if next_action["next_action"] == "confirm":
                self.memory_manager.update_session(current_session_id, {
                    "awaiting_confirmation": True,
                })
                return {
                    "status": "awaiting_confirmation",
                    "response": self._confirmation_message(session_state),
                }

            if next_action["next_action"] == "complete":
                return {
                    "status": "completed",
                    "response": tr("reservation_complete"),
                }

            field = next_action["field"]
            if field:
                reasoning_state = self.conversation_state_manager.record_question(reasoning_state, field)
                self.memory_manager.update_session(current_session_id, reasoning_state)

            return {
                "status": "awaiting_input",
                "response": self._question_for_field(field),
                "field": field,
                "next_action": next_action["next_action"],
            }

        if action == "save_reservation":
            return {
                "status": "complete",
                "response": tr("reservation_ready"),
                "reservation": {
                    "name": session_state.get("name"),
                    "people": session_state.get("people"),
                    "date": session_state.get("date"),
                    "time": session_state.get("time"),
                },
            }

        return {
            "status": "unknown_action",
            "response": tr("no_available_step"),
        }

    async def handle_confirmation(
        self,
        user_message: str,
        session_id: str,
        owner_customer_id=None,
        db=None,
    ) -> dict[str, Any]:
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
        editing_field = session.get("editing_field")

        if editing_field in self.EDITABLE_FIELDS:
            if editing_field == "date":
                user_message = continue_date(user_message, session, clock=self.clock)
                year = inferred_year(user_message, clock=self.clock)
                if year is not None:
                    return self._request_explicit_create_year(session_id, year, confirmation=True)
            value = self._infer_value_for_field(editing_field, user_message)
            if value is not None:
                rejection = self._validate_confirmation_datetime_edit(session_id, editing_field, value)
                if rejection is not None:
                    return rejection
                updated_session = self._apply_confirmation_edit(
                    session_id,
                    editing_field,
                    value,
                )
                return self._confirmation_response(updated_session)

            return {
                "status": "awaiting_confirmation",
                "response": self._question_for_edit_field(editing_field),
            }

        intent, field = self._detect_confirmation_intent(user_message)

        if intent == CONFIRM:
            if owner_customer_id is None:
                return {
                    "status": "awaiting_confirmation",
                    "response": tr("customer_unavailable"),
                }
            if db is None:
                return {
                    "status": "awaiting_confirmation",
                    "response": tr("reservation_service_unavailable"),
                }

            snapshot = self.memory_manager.snapshot_conversation(session_id)
            canonical_values = {}
            for field_name in self.EDITABLE_FIELDS:
                canonical_value = self._normalize_and_validate_field(
                    field_name,
                    session.get(field_name),
                )
                if canonical_value is None:
                    self.memory_manager.update_session(
                        session_id,
                        {
                            "awaiting_confirmation": True,
                            "editing_field": field_name,
                        },
                    )
                    return {
                        "status": "awaiting_confirmation",
                        "response": self._question_for_edit_field(field_name),
                        "invalid_input": True,
                    }
                canonical_values[field_name] = canonical_value
            reservation_data = ReservationCreate(**canonical_values)
            if self.workflow_state_service is None:
                before_mutation = None
            else:
                def mark_mutation():
                    self.workflow_state_service.begin_mutation(
                        db,
                        owner_customer_id=owner_customer_id,
                        memory_key=session_id,
                        operation="create",
                    )

                before_mutation = mark_mutation
            try:
                create_kwargs = {"owner_customer_id": owner_customer_id}
                if before_mutation is not None:
                    create_kwargs["before_mutation"] = before_mutation
                reservation = self.reservation_service.create_reservation(
                    db, reservation_data, **create_kwargs
                )
                reservation_reference = require_canonical_public_reference(
                    reservation.reference
                )
            except PastReservationDateError:
                self.memory_manager.remove_session_keys(session_id, ("date",))
                self.memory_manager.update_session(
                    session_id,
                    {
                        "date": None,
                        "awaiting_confirmation": True,
                        "editing_field": "date",
                    },
                )
                return {
                    "status": "awaiting_confirmation",
                    "response": tr("past_reservation_date"),
                    "invalid_input": True,
                }
            except PastReservationTimeError:
                self.memory_manager.remove_session_keys(session_id, ("time",))
                self.memory_manager.update_session(
                    session_id,
                    {
                        "time": None,
                        "awaiting_confirmation": True,
                        "editing_field": "time",
                    },
                )
                return {
                    "status": "awaiting_confirmation",
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
                    operation="create",
                )
                raise
            except TransactionSessionUnusableError:
                publish_reservation_persistence_blocker(
                    self.memory_manager,
                    session_id,
                    snapshot,
                    status=SESSION_UNUSABLE,
                    operation="create",
                )
                raise
            except PersistenceOperationError:
                self.memory_manager.replace_conversation(session_id, snapshot)
                raise

            publication_failed = False
            try:
                publish_create_success(
                    self.memory_manager,
                    session_id,
                    snapshot,
                    reservation,
                )
            except Exception:
                publication_failed = True
            if publication_failed:
                publish_post_commit_memory_guard(
                    self.memory_manager,
                    session_id,
                    snapshot,
                    operation="create",
                )
            try:
                response = self._create_success_response(reservation_reference)
            except Exception:
                response = tr("committed_format_fallback")
            return {
                "status": "completed",
                "response": response,
                "reservation_operation": ReservationOperationResult(
                    ReservationOperationType.CREATED,
                    reservation_reference,
                ),
            }

        if intent == REJECT:
            self.memory_manager.replace_reservation_workflow_state(
                session_id,
                {},
            )
            return {
                "status": "rejected",
                "response": tr("create_rejected"),
            }

        if intent == EDIT_FIELD and field:
            if field == "date":
                user_message = continue_date(user_message, session, clock=self.clock)
                year = inferred_year(user_message, clock=self.clock)
                if year is not None:
                    return self._request_explicit_create_year(session_id, year, confirmation=True)
            value = await self._extract_direct_edit_value(field, user_message)
            if value is not None:
                rejection = self._validate_confirmation_datetime_edit(session_id, field, value)
                if rejection is not None:
                    return rejection
                updated_session = self._apply_confirmation_edit(session_id, field, value)
                return self._confirmation_response(updated_session)

            self.memory_manager.update_session(session_id, {
                "awaiting_confirmation": True,
                "editing_field": field,
            })
            return {
                "status": "awaiting_confirmation",
                "response": self._question_for_edit_field(field),
            }

        return {
            "status": "awaiting_confirmation",
            "response": self._confirmation_message(session),
            "invalid_input": True,
        }

    def _detect_confirmation_intent(self, user_message: str) -> tuple[str | None, str | None]:
        normalized = normalize_indonesian_text(user_message)
        confirmation = parse_confirmation(user_message)
        if confirmation == "reject":
            return REJECT, None
        if confirmation == "confirm":
            return CONFIRM, None

        change_words = {
            "ubah",
            "ganti",
            "edit",
            "perbaiki",
            "koreksi",
            "jadi",
            "menjadi",
            "pindah",
            "tambah",
            "kurang",
            "geser",
        }
        if re.search(r"\batas nama\b", normalized):
            return EDIT_FIELD, "name"
        if change_words.intersection(normalized.split()):
            field = self._detect_edit_field(normalized)
            if field is not None:
                return EDIT_FIELD, field

        if not change_words.intersection(normalized.split()):
            return None, None

        return EDIT_FIELD, self._detect_edit_field(normalized)

    def _detect_edit_field(self, user_message: str) -> str | None:
        return parse_target_field(user_message)

    async def _extract_direct_edit_value(self, field_name: str, user_message: str) -> Any:
        extracted = await self.entity_extractor.extract(user_message)
        extracted_value = extracted.get(field_name)
        if extracted_value is not None:
            return self._normalize_edit_value(field_name, extracted_value)

        if field_name == "name":
            return self._extract_direct_name_value(user_message)

        if field_name == "people":
            return parse_people_count(user_message)

        if field_name == "date":
            parsed_date = DatetimeParser.parse_date(user_message, clock=self.clock)
            if parsed_date:
                return self._normalize_and_validate_field(field_name, parsed_date)
            return None

        if field_name == "time":
            parsed_time = DatetimeParser.parse_time(user_message)
            if parsed_time:
                return self._normalize_and_validate_field(field_name, parsed_time)
            return None

        return None

    def _extract_direct_name_value(self, user_message: str) -> str | None:
        patterns = (
            r"(?:ubah|ganti|edit|perbaiki|koreksi)\s+(?:nama|atas nama)(?:\s+(?:menjadi|jadi|ke))?\s+(.+)$",
            r"(?:nama|atas nama)\s+(?:menjadi|jadi|ke)\s+(.+)$",
            r"(?:nama|namanya|atas nama)\s+(?:ubah|ganti)\s+(.+)$",
        )

        for pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                try:
                    return normalize_natural_reservation_name(match.group(1))
                except InputValidationError:
                    return None

        return None

    def _normalize_edit_value(self, field_name: str, value: Any) -> Any:
        return self._normalize_and_validate_field(field_name, value)

    def _apply_confirmation_edit(self, session_id: str, field_name: str, value: Any) -> dict[str, Any]:
        self.memory_manager.update_session(session_id, {field_name: value})
        updated_session = self.memory_manager.get_session(session_id)
        updated_session["editing_field"] = None
        updated_session["awaiting_confirmation"] = True
        return updated_session

    def _validate_confirmation_datetime_edit(
        self, session_id: str, field: str, value: Any,
    ) -> dict[str, Any] | None:
        if field not in {"date", "time"}:
            return None
        session = self.memory_manager.get_session(session_id)
        candidate = {**session, field: value}
        try:
            if candidate.get("date") and candidate.get("time"):
                self.reservation_service.validate_new_reservation_datetime(candidate["date"], candidate["time"])
            elif candidate.get("date"):
                self.reservation_service.validate_new_reservation_date(candidate["date"])
            return None
        except PastReservationDateError:
            response = tr("past_reservation_date")
        except PastReservationTimeError:
            response = tr("past_reservation_time")
        self.memory_manager.update_session(session_id, {
            "awaiting_confirmation": True, "editing_field": field,
        })
        return {"status": "awaiting_confirmation", "response": response, "invalid_input": True}

    def _request_explicit_create_year(
        self, session_id: str, year: int, *, confirmation: bool = False,
    ) -> dict[str, Any]:
        # Keep other collected fields, but never promote a parser-inferred year
        # to an accepted date. An edit keeps its old date until clarified; its
        # editing gate prevents a bare "yes" from committing that old value.
        if confirmation:
            self.memory_manager.update_session(session_id, {
                "awaiting_confirmation": True, "editing_field": "date",
            })
        else:
            state = dict(self.memory_manager.get_session(session_id))
            state.update(date=None, awaiting_confirmation=False, editing_field=None)
            state = self.conversation_state_manager.record_question(state, "date")
            self.memory_manager.update_session(session_id, state)
        return {
            "status": "awaiting_confirmation" if confirmation else "awaiting_input",
            "response": tr("clarify_create_year", year=year),
            "field": "date",
            "next_action": "ask_date",
            "invalid_input": True,
        }

    def _confirmation_response(self, session_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "awaiting_confirmation",
            "response": self._confirmation_message(session_state),
        }

    def _infer_pending_field(self, session_state: dict[str, Any]) -> str | None:
        asked_fields = session_state.get("asked_fields", [])
        for field in reversed(self.conversation_state_manager.REQUIRED_FIELDS):
            if field in asked_fields and not session_state.get(field):
                return field
        return None

    def _infer_value_for_field(self, field_name: str, user_message: str) -> Any:
        text = user_message
        if field_name == "name":
            try:
                return normalize_natural_reservation_name(text)
            except InputValidationError:
                return None
        if field_name == "people":
            return self._parse_people_candidate(text)
        if field_name == "date":
            return self._normalize_and_validate_field(field_name, text)
        if field_name == "time":
            return self._normalize_and_validate_field(field_name, text)
        return None

    def _normalize_and_validate_field(self, field_name: str, value: Any) -> Any:
        candidate = value
        if field_name == "date" and isinstance(value, str):
            candidate = DatetimeParser.parse_date(value, clock=self.clock) or value
        elif field_name == "time" and isinstance(value, str):
            candidate = DatetimeParser.parse_time(value) or value
        try:
            return validate_reservation_field(field_name, candidate)
        except InputValidationError:
            return None

    def _parse_people_candidate(self, text: str) -> int | None:
        return parse_people_count(text)

    def _confirmation_message(self, session_state: dict[str, Any]) -> str:
        return tr(
            "create_confirmation",
            name=session_state.get("name", "-"),
            people=session_state.get("people", "-"),
            date=format_date(session_state.get("date", "-")),
            time=format_time(session_state.get("time", "-")),
        )

    def _question_for_edit_field(self, field_name: str) -> str:
        questions = {
            "name": "ask_edit_name",
            "people": "ask_edit_people",
            "date": "ask_edit_date",
            "time": "ask_edit_time",
        }
        return tr(questions.get(field_name, "choose_update_field"))

    def _question_for_field(self, field_name: str) -> str:
        questions = {
            "name": "ask_name",
            "people": "ask_people",
            "date": "ask_date",
            "time": "ask_time",
        }
        return tr(questions.get(field_name, "complete_reservation_details"))

    def _clarification_for_field(self, field_name: str, user_message: str) -> str:
        if field_name == "time":
            bare_hour = re.fullmatch(r"\s*([1-9]|1[0-2])\s*[.!?]?\s*", user_message)
            if bare_hour:
                return tr("clarify_bare_hour", hour=bare_hour[1])
        if (
            field_name == "date"
            and DatetimeParser.date_ambiguity(user_message) == "missing_month_year"
        ):
            match = re.search(r"\btanggal\s+([0-9]{1,2})\b", user_message, re.IGNORECASE)
            day = match.group(1) if match else "tersebut"
            return tr("clarify_date_parts", day=day)
        if (
            field_name == "time"
            and DatetimeParser.time_ambiguity(user_message) == "missing_day_period"
        ):
            return tr("clarify_day_period")
        return self._question_for_field(field_name)

    @staticmethod
    def _create_success_response(reservation_reference: str) -> str:
        return tr("create_success", reference=reservation_reference)
