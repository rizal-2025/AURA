from fastapi import APIRouter
from app.schemas.chat import ChatRequest, ChatResponse
from app.agents.orchestrator import AgentOrchestrator
from fastapi import Depends
from sqlalchemy.orm import Session
from app.db.database import get_db

router = APIRouter()

agent = AgentOrchestrator()


@router.post("/chat", response_model=ChatResponse)

async def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
):

    reply = await agent.handle(
        session_id=request.session_id,
        message=request.message,
        db=db,
    )

    return ChatResponse(reply=reply)