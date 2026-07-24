"""Single trusted boundary for HTTP and integration customer chat messages."""

from app.agents.orchestrator import AgentOrchestrator
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.input_validation import (
    normalize_chat_message,
    validate_session_reference,
)
from app.core.ownership import require_owner_customer_id


class AuthenticatedChatService:
    def __init__(self, agent=None):
        self.agent = agent or AgentOrchestrator()

    async def process(self, *, db, customer, session_reference: str, message: str) -> str:
        owner_customer_id = require_owner_customer_id(getattr(customer, "id", None))
        session_reference = validate_session_reference(session_reference)
        message = normalize_chat_message(message)
        memory_key = build_authenticated_memory_key(owner_customer_id, session_reference)
        try:
            self.agent.handoff_service.restore_active_handoff(
                memory_key,
                db,
                owner_customer_id,
            )
        except Exception:
            # Do not let a recovery failure fall through into a reservation or
            # general AI workflow.
            return self.agent.handoff_service.recovery_error_response()

        return await self.agent.handle(
            session_id=memory_key,
            message=message,
            db=db,
            owner_customer_id=owner_customer_id,
        )

    def ticket_status(self, *, db, customer, session_reference: str) -> str:
        """Return customer-scoped active ticket status without AI or state mutation."""
        owner_customer_id = require_owner_customer_id(getattr(customer, "id", None))
        session_reference = validate_session_reference(session_reference)
        memory_key = build_authenticated_memory_key(owner_customer_id, session_reference)
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


authenticated_chat_service = AuthenticatedChatService()
