"""Hidden reservation read and reset endpoints for the isolated demo."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.api.internal_demo_dependencies import (
    require_demo_service_auth,
    require_demo_session_token,
)
from app.api.internal_demo_sessions import require_empty_request_body
from app.db.database import get_db
from app.schemas.demo_reservation_reset import (
    DemoReservationListResponse,
    DemoResetResponse,
)
from app.services.demo_reservation_reset_service import (
    DemoReservationResetService,
    demo_reservation_reset_service,
)


router = APIRouter(
    prefix="/internal/demo",
    include_in_schema=False,
    dependencies=[Depends(require_demo_service_auth)],
)


def get_demo_reservation_reset_service() -> DemoReservationResetService:
    return demo_reservation_reset_service


async def require_no_query_parameters(request: Request) -> None:
    if request.query_params:
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("query",),
                    "msg": "Query parameters are not accepted.",
                }
            ]
        )


@router.get(
    "/reservations",
    response_model=DemoReservationListResponse,
    status_code=status.HTTP_200_OK,
)
def get_demo_reservations(
    session_token: Annotated[str, Depends(require_demo_session_token)],
    response: Response,
    _empty_body: None = Depends(require_empty_request_body),
    _no_query: None = Depends(require_no_query_parameters),
    db: Session = Depends(get_db),
    service: DemoReservationResetService = Depends(
        get_demo_reservation_reset_service
    ),
) -> DemoReservationListResponse:
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
    _empty_body: None = Depends(require_empty_request_body),
    _no_query: None = Depends(require_no_query_parameters),
    db: Session = Depends(get_db),
    service: DemoReservationResetService = Depends(
        get_demo_reservation_reset_service
    ),
) -> DemoResetResponse:
    result = await service.reset(
        db,
        raw_session_token=session_token,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
