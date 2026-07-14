from fastapi import APIRouter

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.ai.factory import get_ai_provider
from app.services.intent.classifier import IntentClassifier
from app.services.reservation.extractor import ReservationExtractor

router = APIRouter(
    prefix="/chat",
    tags=["Chat"]
)

provider = get_ai_provider()


@router.post(
    "",
    response_model=ChatResponse
)
async def chat(request: ChatRequest):

    reply = await provider.chat(request.message)

    return ChatResponse(
        reply=reply
    )

@router.post("/intent")
async def detect_intent(request: ChatRequest):

    classifier = IntentClassifier()

    result = await classifier.classify(request.message)

    return result

@router.post("/reservation")
async def reservation(request: ChatRequest):

    extractor = ReservationExtractor()

    return await extractor.extract(request.message)