import re

from app.agents.workflow import AgentWorkflow
from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.view_reservation_agent import ViewReservationAgent
from app.agents.result import AgentTurnResult, ReservationOperationResult
from app.brain.classifier import IntentClassifier
from app.brain.indonesian_nlu import parse_confirmation
from app.brain.memory_manager import MemoryManager
from app.brain.planner import Planner
from app.brain.reservation_memory import (
    has_reservation_persistence_blocker,
    reservation_persistence_blocker_response,
)
from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.core.logger import logger
from app.core.locale import current_locale, tr
from app.core.memory_errors import ConversationMemoryError
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.memory.session import memory
from app.services.ai.factory import get_ai_provider
from app.services.conversation.general_conversation import (
    GeneralConversationService,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.handoff import HandoffDetector, HandoffService


class AgentOrchestrator:

    def __init__(
        self,
        *,
        ai_provider=None,
        provider_request_id: str | None = None,
    ):
        self._ai = ai_provider or get_ai_provider()
        self.provider_request_id = provider_request_id
        # Keep classification and response generation on the same approved
        # provider configuration while retaining their separate client
        # boundaries and prompts.
        classifier_provider = ai_provider or get_ai_provider()
        self.intent_classifier = IntentClassifier(provider=classifier_provider)
        self.general_conversation_service = GeneralConversationService(
            self._ai,
        )
        self.planner = Planner()
        self.memory_manager = MemoryManager()
        self.workflow_state_service = ConversationWorkflowStateService(
            self.memory_manager,
        )
        self.workflow = AgentWorkflow(
            memory_manager=self.memory_manager,
            workflow_state_service=self.workflow_state_service,
        )
        self.view_reservation_agent = ViewReservationAgent()
        self.update_reservation_agent = UpdateReservationAgent(
            memory_manager=self.memory_manager,
            workflow_state_service=self.workflow_state_service,
        )
        self.cancel_reservation_agent = CancelReservationAgent(
            memory_manager=self.memory_manager,
            workflow_state_service=self.workflow_state_service,
        )
        self.handoff_service = HandoffService(self.memory_manager)

    @property
    def ai(self):
        return self._ai

    @ai.setter
    def ai(self, provider) -> None:
        """Keep legacy test/integration injection wired to general chat."""

        self._ai = provider
        if hasattr(self, "general_conversation_service"):
            self.general_conversation_service.provider = provider
        else:
            self.general_conversation_service = GeneralConversationService(
                provider
            )

    def seed_general_conversation_history(
        self,
        session_id: str,
        history,
    ) -> None:
        """Seed already session-scoped, persisted demo history."""

        self.memory_manager.update_session(
            session_id,
            {
                "general_conversation_history": (
                    self.general_conversation_service.bounded_history(history)
                )
            },
        )

    async def handle_turn(
        self,
        session_id: str,
        message: str,
        db,
        owner_customer_id=None,
    ) -> AgentTurnResult:
        result = await self._handle_raw(
            session_id,
            message,
            db,
            owner_customer_id,
        )
        if type(result) is AgentTurnResult:
            return result
        return AgentTurnResult(reply=result)

    async def handle(
        self,
        session_id: str,
        message: str,
        db,
        owner_customer_id=None,
    ) -> str:
        """Compatibility text adapter; authenticated code uses handle_turn."""

        result = await self.handle_turn(
            session_id,
            message,
            db,
            owner_customer_id,
        )
        return result.reply

    async def _handle_raw(
        self,
        session_id: str,
        message: str,
        db,
        owner_customer_id=None,
    ):
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()

        if self.handoff_service.is_required(session_id):
            if self._is_handoff_status_request(message):
                return self.handoff_service.status_response(session_id)
            return self.handoff_service.waiting_response(session_id)

        blocked_state = self.memory_manager.get_session(session_id)
        active_workflow = self._has_active_reservation_workflow(blocked_state)
        detected_reservation_intent = (
            IntentClassifier.detect_reservation_intent(message)
        )
        explicit_transactional_intent = detected_reservation_intent in {
            "reservation",
            "view_reservation",
            "update_reservation",
            "cancel_reservation",
        }
        reservation_mutations_blocked = has_reservation_persistence_blocker(
            self.memory_manager,
            session_id,
            blocked_state,
        )

        if (
            not active_workflow
            and not explicit_transactional_intent
            and HandoffDetector.is_explicit_human_request(message)
        ):
            self._create_handoff(session_id, "explicit_human_request", 1, db, owner_customer_id)
            return self.handoff_service.explicit_response(session_id)

        if (
            not active_workflow
            and not explicit_transactional_intent
            and HandoffDetector.is_frustrated(message)
        ):
            self._create_handoff(session_id, "customer_frustration", 1, db, owner_customer_id)
            return self.handoff_service.required_response(session_id)
        if reservation_mutations_blocked:
            if self._is_view_reservation_request(message, {}):
                return await self._view_reservations(
                    db,
                    session_id,
                    owner_customer_id,
                )
            if detected_reservation_intent in {
                "reservation",
                "update_reservation",
                "cancel_reservation",
            }:
                return reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    blocked_state,
                )
            if (
                active_workflow
                and not self._is_safe_blocked_non_mutating_message(message)
            ):
                return reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    blocked_state,
                )
            if HandoffDetector.is_ambiguous_reservation_action(message):
                return reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    blocked_state,
                )
        if not active_workflow and HandoffDetector.is_ambiguous_reservation_action(message):
            attempt_count = self.handoff_service.record_ambiguity(session_id)
            if attempt_count >= 2:
                self._create_handoff(session_id, "ambiguous_intent", attempt_count, db, owner_customer_id)
                return self.handoff_service.required_response(session_id)
            return tr("ambiguous_action")

        if (
            not active_workflow
            and HandoffDetector.is_obvious_nonsense_shape(message)
        ):
            # Obvious synthetic/gibberish shapes are deterministic. Apply this
            # narrow guard before the provider-backed classifier so they cannot
            # inherit an earlier ambiguity/misunderstanding counter and become
            # a handoff. Natural low-confidence text keeps its escalation path.
            self._reset_intent_attempts(session_id)
            return tr("unknown_request")

        try:
            return await self._handle_authenticated(
                session_id,
                message,
                db,
                owner_customer_id,
                reservation_mutations_blocked=reservation_mutations_blocked,
            )
        except (
            ConversationMemoryError,
            PersistenceOperationError,
            PersistenceOutcomeUnknownError,
            TransactionSessionUnusableError,
        ):
            # Persistence adapters own the safe channel-specific response.
            # Never turn a transaction failure into a memory-only handoff.
            raise
        except Exception:
            self._create_handoff(session_id, "internal_error", 1, db, owner_customer_id)
            logger.error("HANDOFF TRANSITION: category=internal_error")
            return self.handoff_service.required_response(session_id)

    async def _handle_authenticated(
        self,
        session_id: str,
        message: str,
        db,
        owner_customer_id=None,
        reservation_mutations_blocked: bool = False,
    ):
        # Authenticated chat must never create or read unscoped conversation
        # memory. The API supplies an owner-scoped internal key only after the
        # bearer token has been validated.
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()

        session = self.memory_manager.get_session(session_id)
        session_payload = dict(session)
        session_payload["session_id"] = session_id

        logger.info(
            "SESSION TRANSITION: intent=%s awaiting_confirmation=%s update_stage=%s "
            "cancel_stage=%s",
            session.get("intent"),
            bool(session.get("awaiting_confirmation")),
            session.get("update_reservation_stage"),
            session.get("cancel_reservation_stage"),
        )

        detected_reservation_intent = IntentClassifier.detect_reservation_intent(
            message
        )
        if (
            self._is_update_reservation_active(session_payload)
            and detected_reservation_intent == "cancel_reservation"
            and not reservation_mutations_blocked
        ):
            self._clear_update_selection_state(session)
            self._reset_intent_attempts(session_id)
            return await self._cancel_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )
        if (
            self._is_cancel_reservation_active(session_payload)
            and detected_reservation_intent == "update_reservation"
            and not reservation_mutations_blocked
        ):
            self._clear_cancel_selection_state(session)
            self._reset_intent_attempts(session_id)
            return await self._update_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )

        if (
            self._is_update_reservation_active(session_payload)
            and not reservation_mutations_blocked
        ):
            self._reset_intent_attempts(session_id)
            return await self._update_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )

        if (
            self._is_cancel_reservation_active(session_payload)
            and not reservation_mutations_blocked
        ):
            self._reset_intent_attempts(session_id)
            return await self._cancel_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )

        if self._is_update_reservation_request(message, session_payload):
            if reservation_mutations_blocked:
                return reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    session,
                )
            self._reset_intent_attempts(session_id)
            return await self._update_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )

        if self._is_cancel_reservation_request(message, session_payload):
            if reservation_mutations_blocked:
                return reservation_persistence_blocker_response(
                    self.memory_manager,
                    session_id,
                    session,
                )
            self._reset_intent_attempts(session_id)
            return await self._cancel_reservation(
                db,
                session_id,
                message,
                owner_customer_id,
            )

        if self._is_view_reservation_request(message, session_payload):
            self._reset_intent_attempts(session_id)
            return await self._view_reservations(db, session_id, owner_customer_id)

        if (
            not reservation_mutations_blocked
            and (
                session_payload.get("awaiting_confirmation")
                or (
                    session_payload.get("intent") == "reservation"
                    and not session_payload.get("completed")
                )
            )
        ):
            intent = "reservation"
            confidence = session_payload.get("intent_confidence", 0.0)
        else:
            if (
                parse_confirmation(message) is not None
                and len(message.split()) <= 3
            ):
                # Confirmation fragments are meaningful only inside the
                # deterministic state machine. Never ask the LLM to invent
                # context for a context-free "Yes", "No", or equivalent.
                self._reset_intent_attempts(session_id)
                return tr("unknown_request")

            try:
                classifier_kwargs = {}
                provider_request_id = getattr(
                    self,
                    "provider_request_id",
                    None,
                )
                if provider_request_id is not None:
                    classifier_kwargs["request_id"] = provider_request_id
                intent_result = await self.intent_classifier.classify(
                    message,
                    **classifier_kwargs,
                )
            except Exception as error:
                try:
                    provider = getattr(self.intent_classifier, "ai", None) or self._ai
                    fallback_emitter = getattr(provider, "emit_fallback", None)
                    categorizer = getattr(provider, "categorize_error", None)
                    if callable(fallback_emitter):
                        reason = (
                            categorizer(error)
                            if callable(categorizer)
                            else "UNKNOWN_ERROR"
                        )
                        fallback_emitter(
                            request_id=provider_request_id,
                            reason=reason,
                            locale=current_locale().value,
                        )
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(
                    "GENERAL CONVERSATION: status=classification_failure "
                    "locale=%s exception=%s",
                    current_locale().value,
                    self._safe_exception_name(error),
                )
                return self._general_conversation_failure(session_id, message)
            intent = intent_result.get("intent", "general")
            confidence = intent_result.get("confidence", 0.0)

            if intent == "reservation" and confidence >= 0.8:
                ai_fields = {
                    field: intent_result[field]
                    for field in ("name", "people", "date", "time")
                    if field in intent_result and session_payload.get(field) is None
                }
                if ai_fields:
                    # The classifier has already deterministically validated
                    # these canonical values.  Only fill missing fields; an AI
                    # fallback can never replace established workflow state.
                    self.memory_manager.update_session(session_id, ai_fields)
                    session_payload.update(ai_fields)

            if intent == "general":
                self._reset_intent_attempts(session_id)
                self.memory_manager.update_session(
                    session_id,
                    {
                        "intent": intent,
                        "intent_confidence": confidence,
                    },
                )
                return await self._general_conversation(session_id, message)

            if HandoffDetector.is_low_confidence(intent, confidence):
                if intent in {"ambiguous", "update_reservation", "cancel_reservation"}:
                    attempt_count = self.handoff_service.record_ambiguity(session_id)
                    if attempt_count >= 2:
                        self._create_handoff(session_id, "ambiguous_intent", attempt_count, db, owner_customer_id)
                        return self.handoff_service.required_response(session_id)
                    return tr("ambiguous_reservation")
                else:
                    attempt_count = self.handoff_service.record_misunderstanding(session_id)
                    if attempt_count >= 2:
                        self._create_handoff(session_id, "repeated_misunderstanding", attempt_count, db, owner_customer_id)
                        return self.handoff_service.required_response(session_id)
                    return tr("unknown_request")
            else:
                self._reset_intent_attempts(session_id)

            if intent == "view_reservation":
                return await self._view_reservations(db, session_id, owner_customer_id)

            if intent == "update_reservation":
                if reservation_mutations_blocked:
                    return reservation_persistence_blocker_response(
                        self.memory_manager,
                        session_id,
                        session,
                    )
                return await self._update_reservation(
                    db,
                    session_id,
                    message,
                    owner_customer_id,
                )

            if intent == "cancel_reservation":
                if reservation_mutations_blocked:
                    return reservation_persistence_blocker_response(
                        self.memory_manager,
                        session_id,
                        session,
                    )
                return await self._cancel_reservation(
                    db,
                    session_id,
                    message,
                    owner_customer_id,
                )

            if intent == "ambiguous":
                attempt_count = self.handoff_service.record_ambiguity(session_id)
                if attempt_count >= 2:
                    self._create_handoff(session_id, "ambiguous_intent", attempt_count, db, owner_customer_id)
                    return self.handoff_service.required_response(session_id)
                return tr("ambiguous_action")

            self.memory_manager.update_session(
                session_id,
                {
                    "intent": intent,
                    "intent_confidence": confidence,
                }
            )

        logger.info(
            "WORKFLOW TRANSITION: intent=%s confidence_available=%s",
            intent,
            confidence is not None,
        )

        if intent == "reservation" and reservation_mutations_blocked:
            return reservation_persistence_blocker_response(
                self.memory_manager,
                session_id,
                session,
            )

        if intent in {"reservation", "check_reservation", "cancel_reservation", "greeting", "general_question"}:
            self._reset_intent_attempts(session_id)
            session = self.memory_manager.get_session(session_id)
            session_payload = dict(session)
            session_payload["session_id"] = session_id
            plan = await self.planner.plan(
                intent_result={
                    "intent": intent,
                    "confidence": confidence,
                },
                conversation_state=session_payload,
            )

            workflow_result = await self.workflow.execute(
                plan,
                session_payload,
                message,
                session_id=session_id,
                owner_customer_id=owner_customer_id,
                db=db,
            )

            if intent == "reservation":
                if workflow_result.get("invalid_input"):
                    attempt_count = self.handoff_service.record_invalid_input(
                        session_id,
                        "create",
                        "confirmation",
                    )
                    if attempt_count >= 3:
                        self._create_handoff(session_id, "repeated_invalid_input", attempt_count, db, owner_customer_id)
                        return self.handoff_service.required_response(session_id)
                else:
                    self.handoff_service.reset_invalid_input(session_id)

            if workflow_result.get("status") == "complete":
                self.memory_manager.update_session(
                    session_id,
                    {"completed": True},
                )
            elif workflow_result.get("status") == "awaiting_input":
                self.memory_manager.update_session(
                    session_id,
                    {
                        workflow_result.get("field"): None,
                    },
                )

            return self._turn_result_from_agent_payload(workflow_result)

        return await self._general_conversation(session_id, message)

    async def _general_conversation(
        self,
        session_id: str,
        message: str,
    ) -> str:
        session = self.memory_manager.get_session(session_id)
        history = session.get("general_conversation_history")
        response_kwargs = {}
        provider_request_id = getattr(self, "provider_request_id", None)
        if provider_request_id is not None:
            response_kwargs["request_id"] = provider_request_id
        reply = await self.general_conversation_service.respond(
            message,
            history,
            **response_kwargs,
        )
        session["general_conversation_history"] = (
            self.general_conversation_service.append_exchange(
                history,
                message,
                reply,
            )
        )
        return reply

    def _general_conversation_failure(
        self,
        session_id: str,
        message: str,
    ) -> str:
        session = self.memory_manager.get_session(session_id)
        history = session.get("general_conversation_history")
        reply = self.general_conversation_service.failure_reply()
        session["general_conversation_history"] = (
            self.general_conversation_service.append_exchange(
                history,
                message,
                reply,
            )
        )
        self._reset_intent_attempts(session_id)
        return reply

    def _is_view_reservation_request(
        self,
        message: str,
        session_state: dict,
    ) -> bool:
        if session_state.get("awaiting_confirmation"):
            return False

        return (
            IntentClassifier.detect_reservation_intent(message)
            == "view_reservation"
        )

    def _has_active_reservation_workflow(self, session_state: dict) -> bool:
        return bool(
            session_state.get("awaiting_confirmation")
            or (
                session_state.get("intent") == "reservation"
                and not session_state.get("completed")
            )
            or self._is_update_reservation_active(session_state)
            or self._is_cancel_reservation_active(session_state)
        )

    def _is_update_reservation_active(self, session_state: dict) -> bool:
        return (
            not session_state.get("awaiting_confirmation")
            and bool(session_state.get("update_reservation_stage"))
        )

    def _is_update_reservation_request(
        self,
        message: str,
        session_state: dict,
    ) -> bool:
        if session_state.get("awaiting_confirmation"):
            return False

        return (
            IntentClassifier.detect_reservation_intent(message)
            == "update_reservation"
        )

    def _is_cancel_reservation_active(self, session_state: dict) -> bool:
        return (
            not session_state.get("awaiting_confirmation")
            and bool(session_state.get("cancel_reservation_stage"))
        )

    def _is_cancel_reservation_request(
        self,
        message: str,
        session_state: dict,
    ) -> bool:
        if session_state.get("awaiting_confirmation"):
            return False

        return (
            IntentClassifier.detect_reservation_intent(message)
            == "cancel_reservation"
        )

    @staticmethod
    def _clear_update_selection_state(session: dict) -> None:
        session.pop("pending_reservation_day", None)
        session["update_reservation_stage"] = None
        session["update_reservation_candidate_references"] = []
        session["update_reservation_page_cursor"] = None
        session["update_reservation_page_has_more"] = None
        session["reservation_reference"] = None
        session["editing_field"] = None

    @staticmethod
    def _clear_cancel_selection_state(session: dict) -> None:
        session.pop("pending_reservation_day", None)
        session["cancel_reservation_stage"] = None
        session["cancel_reservation_candidate_references"] = []
        session["cancel_reservation_page_cursor"] = None
        session["cancel_reservation_page_has_more"] = None
        session["cancel_reservation_reference"] = None

    async def _view_reservations(
        self,
        db,
        session_id: str,
        owner_customer_id,
    ) -> AgentTurnResult:
        if not self._has_authenticated_owner(owner_customer_id):
            return AgentTurnResult(reply=self._authorization_error_response())
        result = await self.view_reservation_agent.run(
            db,
            session_id,
            owner_customer_id,
        )
        return self._turn_result_from_agent_payload(result)

    async def _update_reservation(
        self,
        db,
        session_id: str,
        message: str,
        owner_customer_id,
    ) -> AgentTurnResult:
        if not self._has_authenticated_owner(owner_customer_id):
            return AgentTurnResult(reply=self._authorization_error_response())
        result = await self.update_reservation_agent.run(
            db,
            session_id,
            message,
            owner_customer_id,
        )
        session = self.memory_manager.get_session(session_id)
        if result.get("invalid_input"):
            attempt_count = self.handoff_service.record_invalid_input(
                session_id,
                "update",
                session.get("update_reservation_stage"),
            )
            if attempt_count >= 3:
                self._create_handoff(session_id, "repeated_invalid_input", attempt_count, db, owner_customer_id)
                return self.handoff_service.required_response(session_id)
        else:
            self.handoff_service.reset_invalid_input(session_id)
        logger.info(
            "UPDATE RESERVATION STATE: status=%s stage=%s editing_field=%s",
            result.get("status"),
            session.get("update_reservation_stage"),
            session.get("editing_field"),
        )
        return self._turn_result_from_agent_payload(result)

    async def _cancel_reservation(
        self,
        db,
        session_id: str,
        message: str,
        owner_customer_id,
    ) -> AgentTurnResult:
        if not self._has_authenticated_owner(owner_customer_id):
            return AgentTurnResult(reply=self._authorization_error_response())
        result = await self.cancel_reservation_agent.run(
            db,
            session_id,
            message,
            owner_customer_id,
        )
        session = self.memory_manager.get_session(session_id)
        if result.get("invalid_input"):
            attempt_count = self.handoff_service.record_invalid_input(
                session_id,
                "cancel",
                session.get("cancel_reservation_stage"),
            )
            if attempt_count >= 3:
                self._create_handoff(session_id, "repeated_invalid_input", attempt_count, db, owner_customer_id)
                return self.handoff_service.required_response(session_id)
        else:
            self.handoff_service.reset_invalid_input(session_id)
        logger.info(
            "CANCEL RESERVATION STATE: status=%s stage=%s",
            result.get("status"),
            session.get("cancel_reservation_stage"),
        )
        return self._turn_result_from_agent_payload(result)

    @staticmethod
    def _turn_result_from_agent_payload(result: dict) -> AgentTurnResult:
        reply = result.get("response", "")
        operation = result.get("reservation_operation")
        if operation is not None and type(operation) is not ReservationOperationResult:
            raise ValueError("INVALID_RESERVATION_OPERATION")
        return AgentTurnResult(
            reply=reply,
            reservation_operation=operation,
        )

    @staticmethod
    def _has_authenticated_owner(owner_customer_id) -> bool:
        try:
            require_owner_customer_id(owner_customer_id)
        except MissingOwnerCustomerError:
            return False
        return True

    @staticmethod
    def _authorization_error_response() -> str:
        return tr("authorization_required")

    def _reset_intent_attempts(self, session_id: str) -> None:
        self.handoff_service.reset_misunderstandings(session_id)
        self.handoff_service.reset_ambiguity(session_id)

    def _create_handoff(self, session_id, category, attempt_count, db, owner_customer_id) -> None:
        self.handoff_service.require_handoff(
            session_id,
            category,
            attempt_count,
            db=db,
            owner_customer_id=owner_customer_id,
        )

    @staticmethod
    def _safe_exception_name(error: Exception) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__) or "UnknownError"

    @staticmethod
    def _is_handoff_status_request(message: str) -> bool:
        normalized = " ".join(message.lower().strip().split())
        return normalized in {"status handoff", "status bantuan", "status petugas"}

    @staticmethod
    def _is_safe_blocked_non_mutating_message(message: str) -> bool:
        if IntentClassifier.detect_greeting_intent(message) == "greeting":
            return True
        if IntentClassifier.detect_reservation_intent(message) not in {None, "general"}:
            return False
        normalized = " ".join(message.lower().strip().split())
        informational_terms = (
            "apa",
            "apakah",
            "bagaimana",
            "berapa",
            "bisakah",
            "bolehkah",
            "di mana",
            "dimana",
            "jam buka",
            "kapan",
            "mengapa",
            "kenapa",
        )
        return normalized.endswith("?") or any(
            term in normalized.split() or term in normalized
            for term in informational_terms
        )
