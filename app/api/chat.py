from fastapi import APIRouter
from app.schemas.chat import ChatRequest, ChatResponse
from app.agents.orchestrator import AgentOrchestrator
from fastapi import Depends
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_customer
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

    reply = await agent.handle(
        session_id=request.session_id,
        message=request.message,
        db=db,
        owner_customer_id=current_customer.id,
    )

    return ChatResponse(reply=reply)
