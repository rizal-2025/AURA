import re

from app.agents.workflow import AgentWorkflow
from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.view_reservation_agent import ViewReservationAgent
from app.brain.classifier import IntentClassifier
from app.brain.memory_manager import MemoryManager
from app.brain.planner import Planner
from app.brain.reservation_memory import (
    has_reservation_persistence_blocker,
    reservation_persistence_blocker_response,
)
from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.core.logger import logger
from app.core.memory_errors import ConversationMemoryError
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.memory.session import memory
from app.services.ai.factory import get_ai_provider
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from app.services.handoff import HandoffDetector, HandoffService


class AgentOrchestrator:

    def __init__(self):
        self.ai = get_ai_provider()
        self.intent_classifier = IntentClassifier()
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

    async def handle(
        self,
        session_id: str,
        message: str,
        db,
        owner_customer_id=None,
    ):
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()

        explicit_handoff = HandoffDetector.is_explicit_human_request(message)
        if self.handoff_service.is_required(session_id):
            if self._is_handoff_status_request(message):
                return self.handoff_service.status_response(session_id)
            return self.handoff_service.waiting_response(session_id)

        blocked_state = self.memory_manager.get_session(session_id)
        reservation_mutations_blocked = has_reservation_persistence_blocker(
            self.memory_manager,
            session_id,
            blocked_state,
        )

        if explicit_handoff:
            self._create_handoff(session_id, "explicit_human_request", 1, db, owner_customer_id)
            return self.handoff_service.explicit_response(session_id)

        if HandoffDetector.is_frustrated(message):
            self._create_handoff(session_id, "customer_frustration", 1, db, owner_customer_id)
            return self.handoff_service.required_response(session_id)

        current_session = self.memory_manager.get_session(session_id)
        active_workflow = (
            current_session.get("awaiting_confirmation")
            or self._is_update_reservation_active(current_session)
            or self._is_cancel_reservation_active(current_session)
        )
        if reservation_mutations_blocked:
            if self._is_view_reservation_request(message, {}):
                return await self._view_reservations(
                    db,
                    session_id,
                    owner_customer_id,
                )
            detected_reservation_intent = (
                IntentClassifier.detect_reservation_intent(message)
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
            return "Apakah Anda ingin mengubah atau membatalkan reservasi?"

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
            and session_payload.get("intent") == "reservation"
            and not session_payload.get("completed")
        ):
            intent = session_payload["intent"]
            confidence = session_payload.get("intent_confidence", 0.0)
        else:
            intent_result = await self.intent_classifier.classify(message)
            intent = intent_result.get("intent", "general")
            confidence = intent_result.get("confidence", 0.0)

            safe_blocked_general = (
                reservation_mutations_blocked
                and intent == "general"
                and self._is_safe_blocked_non_mutating_message(message)
            )
            if safe_blocked_general:
                self._reset_intent_attempts(session_id)
            elif intent == "general" and HandoffDetector.is_deterministically_misunderstood(message):
                attempt_count = self.handoff_service.record_misunderstanding(session_id)
                if attempt_count >= 2:
                    self._create_handoff(session_id, "repeated_misunderstanding", attempt_count, db, owner_customer_id)
                    return self.handoff_service.required_response(session_id)
                return "Maaf, saya belum memahami permintaan Anda. Bisa dijelaskan kembali?"

            if not safe_blocked_general and HandoffDetector.is_low_confidence(intent, confidence):
                if intent == "general" and HandoffDetector.is_safe_non_action_message(message):
                    self._reset_intent_attempts(session_id)
                elif intent in {"ambiguous", "update_reservation", "cancel_reservation"}:
                    attempt_count = self.handoff_service.record_ambiguity(session_id)
                    if attempt_count >= 2:
                        self._create_handoff(session_id, "ambiguous_intent", attempt_count, db, owner_customer_id)
                        return self.handoff_service.required_response(session_id)
                    return "Saya belum yakin tindakan reservasi yang Anda maksud. Mohon jelaskan kembali."
                else:
                    attempt_count = self.handoff_service.record_misunderstanding(session_id)
                    if attempt_count >= 2:
                        self._create_handoff(session_id, "repeated_misunderstanding", attempt_count, db, owner_customer_id)
                        return self.handoff_service.required_response(session_id)
                    return "Maaf, saya belum memahami permintaan Anda. Bisa dijelaskan kembali?"
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
                return "Apakah Anda ingin mengubah atau membatalkan reservasi?"

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

            response_text = workflow_result.get("response", "")

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

            return response_text

        try:
            return await self.ai.chat(message)
        except Exception as error:
            logger.error(
                "AI PROVIDER FAILURE: operation=general_chat exception=%s",
                self._safe_exception_name(error),
            )
            raise

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

    async def _view_reservations(self, db, session_id: str, owner_customer_id) -> str:
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()
        result = await self.view_reservation_agent.run(
            db,
            session_id,
            owner_customer_id,
        )
        return result.get("response", "")

    async def _update_reservation(
        self,
        db,
        session_id: str,
        message: str,
        owner_customer_id,
    ) -> str:
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()
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
        return result.get("response", "")

    async def _cancel_reservation(
        self,
        db,
        session_id: str,
        message: str,
        owner_customer_id,
    ) -> str:
        if not self._has_authenticated_owner(owner_customer_id):
            return self._authorization_error_response()
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
        return result.get("response", "")

    @staticmethod
    def _has_authenticated_owner(owner_customer_id) -> bool:
        try:
            require_owner_customer_id(owner_customer_id)
        except MissingOwnerCustomerError:
            return False
        return True

    @staticmethod
    def _authorization_error_response() -> str:
        return "Identitas pelanggan tidak valid atau telah kedaluwarsa."

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
