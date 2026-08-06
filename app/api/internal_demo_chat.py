"""Hidden server-to-server endpoint for isolated demo chat."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.internal_demo_dependencies import (
    get_demo_rate_limit_service,
    require_demo_service_auth,
    require_demo_client_subject,
    require_no_query_parameters,
    require_demo_session_token,
)
from app.db.database import get_db
from app.schemas.demo_chat import DemoChatRequest, DemoChatResponse
from app.services.demo_chat_service import (
    DemoChatService,
    demo_chat_service,
)
from app.services.demo_rate_limit_service import (
    DemoRateLimitAction,
    DemoRateLimitService,
)


router = APIRouter(
    prefix="/internal/demo",
    include_in_schema=False,
    dependencies=[Depends(require_demo_service_auth)],
)


def get_demo_chat_service() -> DemoChatService:
    return demo_chat_service


@router.post(
    "/chat",
    response_model=DemoChatResponse,
    status_code=status.HTTP_200_OK,
)
async def post_demo_chat(
    request: DemoChatRequest,
    session_token: Annotated[
        str,
        Depends(require_demo_session_token),
    ],
    response: Response,
    client_subject: Annotated[str, Depends(require_demo_client_subject)],
    _no_query: None = Depends(require_no_query_parameters),
    db: Session = Depends(get_db),
    service: DemoChatService = Depends(get_demo_chat_service),
    rate_limits: DemoRateLimitService = Depends(
        get_demo_rate_limit_service
    ),
) -> DemoChatResponse:
    token_digest = rate_limits.resolve_active_session_digest(
        db,
        session_token,
    )
    rate_limits.enforce(
        db,
        action=DemoRateLimitAction.CHAT,
        session_token_digest=token_digest,
        client_subject_digest=client_subject,
    )
    result = await service.process(
        db,
        raw_session_token=session_token,
        message=request.message,
        request_id=request.request_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
