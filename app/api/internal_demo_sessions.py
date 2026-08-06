"""Hidden server-to-server endpoints for demo session lifecycle."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.api.internal_demo_dependencies import (
    get_demo_session_service,
    get_demo_rate_limit_service,
    require_demo_service_auth,
    require_demo_client_subject,
    require_demo_session_token,
)
from app.db.database import get_db
from app.schemas.demo_session import (
    DemoSessionCreateResponse,
    DemoSessionCurrentResponse,
)
from app.services.demo_session_service import DemoSessionService
from app.services.demo_rate_limit_service import (
    DemoRateLimitAction,
    DemoRateLimitService,
)


router = APIRouter(
    prefix="/internal/demo",
    include_in_schema=False,
    dependencies=[Depends(require_demo_service_auth)],
)


async def require_empty_request_body(request: Request) -> None:
    if await request.body():
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("body",),
                    "msg": "Request body is not accepted.",
                }
            ]
        )


@router.post(
    "/sessions",
    response_model=DemoSessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_demo_session(
    response: Response,
    client_subject: Annotated[str, Depends(require_demo_client_subject)],
    _empty_body: None = Depends(require_empty_request_body),
    db: Session = Depends(get_db),
    service: DemoSessionService = Depends(get_demo_session_service),
    rate_limits: DemoRateLimitService = Depends(
        get_demo_rate_limit_service
    ),
) -> DemoSessionCreateResponse:
    rate_limits.enforce(
        db,
        action=DemoRateLimitAction.SESSION_CREATE,
        client_subject_digest=client_subject,
    )
    created = service.create_session(db)
    response.headers["Cache-Control"] = "no-store"
    return created


@router.get(
    "/sessions/current",
    response_model=DemoSessionCurrentResponse,
    status_code=status.HTTP_200_OK,
)
def get_current_demo_session(
    session_token: Annotated[
        str,
        Depends(require_demo_session_token),
    ],
    response: Response,
    client_subject: Annotated[str, Depends(require_demo_client_subject)],
    db: Session = Depends(get_db),
    service: DemoSessionService = Depends(get_demo_session_service),
    rate_limits: DemoRateLimitService = Depends(
        get_demo_rate_limit_service
    ),
) -> DemoSessionCurrentResponse:
    token_digest = rate_limits.resolve_active_session_digest(
        db,
        session_token,
    )
    rate_limits.enforce(
        db,
        action=DemoRateLimitAction.SESSION_CURRENT,
        session_token_digest=token_digest,
        client_subject_digest=client_subject,
    )
    current = service.get_current_session(db, session_token)
    response.headers["Cache-Control"] = "no-store"
    return current
