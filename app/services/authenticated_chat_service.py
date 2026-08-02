"""Single trusted boundary for HTTP and integration customer chat messages."""

from app.agents.orchestrator import AgentOrchestrator
from app.agents.result import AgentTurnResult
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.input_validation import (
    normalize_chat_message,
    validate_session_reference,
)
from app.core.memory_errors import ConversationMemoryError
from app.core.ownership import require_owner_customer_id
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from sqlalchemy.orm import Session as SQLAlchemySession
from app.services.conversation_workflow_state_service import WorkflowRestoreOutcome


LEGACY_WORKFLOW_UNAVAILABLE_RESPONSE = (
    "Sesi reservasi sebelumnya tidak dapat dipulihkan. "
    "Silakan mulai kembali dari daftar reservasi Anda."
)


class AuthenticatedChatService:
    def __init__(
        self,
        agent=None,
        lock_manager=None,
        workflow_state_service=None,
    ):
        self.agent = agent if agent is not None else AgentOrchestrator()
        self.lock_manager = (
            lock_manager
            if lock_manager is not None
            else ConversationLockManager()
        )
        self.workflow_state_service = (
            workflow_state_service
            if workflow_state_service is not None
            else getattr(self.agent, "workflow_state_service", None)
        )
        self._workflow_state_service_explicit = (
            workflow_state_service is not None
        )

    @staticmethod
    def _is_postgresql_session(db) -> bool:
        if not isinstance(db, SQLAlchemySession):
            return False
        try:
            return db.get_bind().dialect.name == "postgresql"
        except Exception:
            return False

    async def process_turn(
        self,
        *,
        db,
        customer,
        session_reference: str,
        message: str,
    ) -> AgentTurnResult:
        owner_customer_id = require_owner_customer_id(getattr(customer, "id", None))
        session_reference = validate_session_reference(session_reference)
        message = normalize_chat_message(message)
        memory_key = build_authenticated_memory_key(owner_customer_id, session_reference)
        async with self.lock_manager.hold(memory_key):
            workflow_state_service = self.workflow_state_service
            workflow_persistence_enabled = (
                workflow_state_service is not None
                and (
                    self._workflow_state_service_explicit
                    or self._is_postgresql_session(db)
                )
            )
            if workflow_persistence_enabled:
                restore_outcome = workflow_state_service.restore(
                    db,
                    owner_customer_id=owner_customer_id,
                    memory_key=memory_key,
                )
                if (
                    restore_outcome
                    is WorkflowRestoreOutcome.LEGACY_UNAVAILABLE
                ):
                    return AgentTurnResult(
                        reply=LEGACY_WORKFLOW_UNAVAILABLE_RESPONSE
                    )
            try:
                self.agent.handoff_service.restore_active_handoff(
                    memory_key,
                    db,
                    owner_customer_id,
                )
            except (
                ConversationMemoryError,
                PersistenceOperationError,
                PersistenceOutcomeUnknownError,
                TransactionSessionUnusableError,
            ):
                raise
            except Exception:
                # Do not let a recovery failure fall through into a reservation
                # or general AI workflow.
                response = self.agent.handoff_service.recovery_error_response()
            else:
                try:
                    handler = getattr(self.agent, "handle_turn", None)
                    if handler is None:
                        handler = self.agent.handle
                    response = await handler(
                        session_id=memory_key,
                        message=message,
                        db=db,
                        owner_customer_id=owner_customer_id,
                    )
                except (
                    ConversationMemoryError,
                    PersistenceOperationError,
                    PersistenceOutcomeUnknownError,
                    TransactionSessionUnusableError,
                ):
                    if workflow_persistence_enabled:
                        workflow_state_service.publish(
                            db,
                            owner_customer_id=owner_customer_id,
                            memory_key=memory_key,
                        )
                    raise
                if workflow_persistence_enabled:
                    workflow_state_service.publish(
                        db,
                        owner_customer_id=owner_customer_id,
                        memory_key=memory_key,
                    )
        if type(response) is AgentTurnResult:
            return response
        return AgentTurnResult(reply=response)

    async def process(
        self,
        *,
        db,
        customer,
        session_reference: str,
        message: str,
    ) -> str:
        result = await self.process_turn(
            db=db,
            customer=customer,
            session_reference=session_reference,
            message=message,
        )
        return result.reply

    async def ticket_status(self, *, db, customer, session_reference: str) -> str:
        """Return customer-scoped active ticket status without AI mutation."""
        owner_customer_id = require_owner_customer_id(getattr(customer, "id", None))
        session_reference = validate_session_reference(session_reference)
        memory_key = build_authenticated_memory_key(owner_customer_id, session_reference)
        async with self.lock_manager.hold(memory_key):
            ticket = self.agent.handoff_service.ticket_service.get_active(
                db,
                owner_customer_id=owner_customer_id,
                memory_key=memory_key,
            )
        if ticket is None:
            return "Saat ini Anda tidak memiliki tiket bantuan yang aktif."
        return (
            "Tiket bantuan Anda masih aktif."
            f"\nNomor tiket Anda: {ticket.ticket_number}"
        )


# One explicit production manager is shared by every customer ingress using
# this service inside the current Python process.
conversation_lock_manager = ConversationLockManager()
authenticated_chat_service = AuthenticatedChatService(
    lock_manager=conversation_lock_manager,
)
