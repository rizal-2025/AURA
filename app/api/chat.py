from fastapi import APIRouter
from app.schemas.chat import ChatRequest, ChatResponse
from app.agents.orchestrator import AgentOrchestrator
from fastapi import Depends
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_customer
from app.core.conversation_memory import build_authenticated_memory_key
from app.db.database import get_db
from app.db.models.customer import Customer

router = APIRouter()

agent = AgentOrchestrator()


@router.post("/chat", response_model=ChatResponse)

async def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_customer: Customer = Depends(get_current_customer),
):
    # Authentication has completed before this point. Keep the client supplied
    # session_id as a conversation label, but scope the in-memory state to the
    # authenticated customer.
    memory_key = build_authenticated_memory_key(
        current_customer.id,
        request.session_id,
    )

    try:
        agent.handoff_service.restore_active_handoff(
            memory_key,
            db,
            current_customer.id,
        )
    except Exception:
        # Fail before any classifier, AI provider, or reservation workflow can
        # run. Database and identity details are deliberately not returned.
        return ChatResponse(reply=agent.handoff_service.recovery_error_response())

    reply = await agent.handle(
        session_id=memory_key,
        message=request.message,
        db=db,
        owner_customer_id=current_customer.id,
    )

    return ChatResponse(reply=reply)
