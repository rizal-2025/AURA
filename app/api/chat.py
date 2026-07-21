from fastapi import APIRouter
from app.schemas.chat import ChatRequest, ChatResponse
from fastapi import Depends
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_customer
from app.db.database import get_db
from app.db.models.customer import Customer
from app.services.authenticated_chat_service import authenticated_chat_service

router = APIRouter()

# Compatibility alias used by existing tests and diagnostics. Both HTTP and
# Telegram use this same application-level authenticated chat boundary.
agent = authenticated_chat_service.agent


@router.post("/chat", response_model=ChatResponse)

async def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_customer: Customer = Depends(get_current_customer),
):
    reply = await authenticated_chat_service.process(
        db=db,
        customer=current_customer,
        session_reference=request.session_id,
        message=request.message,
    )

    return ChatResponse(reply=reply)
