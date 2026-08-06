"""Hidden reservation read and reset endpoints for the isolated demo."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.internal_demo_dependencies import (
    get_demo_rate_limit_service,
    require_demo_service_auth,
    require_demo_client_subject,
    require_empty_request_body,
    require_no_query_parameters,
    require_demo_session_token,
)
from app.db.database import get_db
from app.schemas.demo_reservation_reset import (
    DemoReservationListResponse,
    DemoResetResponse,
)
from app.services.demo_reservation_reset_service import (
    DemoReservationResetService,
    demo_reservation_reset_service,
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


def get_demo_reservation_reset_service() -> DemoReservationResetService:
    return demo_reservation_reset_service


@router.get(
    "/reservations",
    response_model=DemoReservationListResponse,
    status_code=status.HTTP_200_OK,
)
def get_demo_reservations(
    session_token: Annotated[str, Depends(require_demo_session_token)],
    response: Response,
    client_subject: Annotated[str, Depends(require_demo_client_subject)],
    _empty_body: None = Depends(require_empty_request_body),
    _no_query: None = Depends(require_no_query_parameters),
    db: Session = Depends(get_db),
    service: DemoReservationResetService = Depends(
        get_demo_reservation_reset_service
    ),
    rate_limits: DemoRateLimitService = Depends(
        get_demo_rate_limit_service
    ),
) -> DemoReservationListResponse:
    token_digest = rate_limits.resolve_active_session_digest(
        db,
        session_token,
    )
    rate_limits.enforce(
        db,
        action=DemoRateLimitAction.RESERVATIONS_READ,
        session_token_digest=token_digest,
        client_subject_digest=client_subject,
    )
    result = service.list_reservations(
        db,
        raw_session_token=session_token,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/reset",
    response_model=DemoResetResponse,
    status_code=status.HTTP_200_OK,
)
async def reset_demo_session_data(
    session_token: Annotated[str, Depends(require_demo_session_token)],
    response: Response,
    client_subject: Annotated[str, Depends(require_demo_client_subject)],
    _empty_body: None = Depends(require_empty_request_body),
    _no_query: None = Depends(require_no_query_parameters),
    db: Session = Depends(get_db),
    service: DemoReservationResetService = Depends(
        get_demo_reservation_reset_service
    ),
    rate_limits: DemoRateLimitService = Depends(
        get_demo_rate_limit_service
    ),
) -> DemoResetResponse:
    token_digest = rate_limits.resolve_active_session_digest(
        db,
        session_token,
    )
    rate_limits.enforce(
        db,
        action=DemoRateLimitAction.RESET,
        session_token_digest=token_digest,
        client_subject_digest=client_subject,
    )
    result = await service.reset(
        db,
        raw_session_token=session_token,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
