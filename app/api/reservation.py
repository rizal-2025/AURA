from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_customer
from app.core.customer_identity import AuthenticatedCustomer
from app.db.database import get_db
from app.schemas.reservation import (
    PublicReservationListResponse,
    PublicReservationResponse,
    ReservationCreate,
    ReservationUpdate,
)
from app.services.reservation.errors import (
    ReservationNotFoundError,
    ReservationReferenceRequestError,
)
from app.services.reservation.public_mapper import (
    map_public_reservation,
    map_public_reservation_list,
)
from app.services.reservation.public_reference import (
    InvalidPublicReservationReferenceError,
    canonicalize_public_reference,
)
from app.services.reservation.service import ReservationService

router = APIRouter(prefix="/reservation", tags=["Reservation"])

service = ReservationService()


def _canonical_request_reference(reference: str) -> str:
    try:
        return canonicalize_public_reference(reference)
    except InvalidPublicReservationReferenceError:
        raise ReservationReferenceRequestError() from None


def _require_reservation(value):
    if value is None:
        raise ReservationNotFoundError()
    return value


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


@router.post("/", response_model=PublicReservationResponse)
def create(
    reservation: ReservationCreate,
    db: Session = Depends(get_db),
    current_customer: AuthenticatedCustomer = Depends(get_current_customer),
):
    return map_public_reservation(
        service.create_reservation(
            db,
            reservation,
            owner_customer_id=current_customer.id,
        )
    )


@router.get("/", response_model=PublicReservationListResponse)
def list_reservations(
    db: Session = Depends(get_db),
    current_customer: AuthenticatedCustomer = Depends(get_current_customer),
):
    reservations, count = service.list_owner_reservations(
        db,
        owner_customer_id=current_customer.id,
        limit=50,
    )
    return map_public_reservation_list(reservations, count=count)


@router.get("/{reference}", response_model=PublicReservationResponse)
def detail(
    reference: str,
    db: Session = Depends(get_db),
    current_customer: AuthenticatedCustomer = Depends(get_current_customer),
):
    canonical_reference = _canonical_request_reference(reference)
    result = service.get_reservation_by_reference(
        db,
        canonical_reference,
        owner_customer_id=current_customer.id,
    )
    return map_public_reservation(_require_reservation(result))


@router.patch("/{reference}", response_model=PublicReservationResponse)
def update(
    reference: str,
    reservation: ReservationUpdate,
    db: Session = Depends(get_db),
    current_customer: AuthenticatedCustomer = Depends(get_current_customer),
):
    canonical_reference = _canonical_request_reference(reference)
    field_name, value = reservation.selected_field()
    result = service.update_reservation_field_by_reference(
        db,
        canonical_reference,
        field_name,
        value,
        owner_customer_id=current_customer.id,
    )
    return map_public_reservation(_require_reservation(result))


@router.post(
    "/{reference}/cancel",
    response_model=PublicReservationResponse,
    dependencies=[Depends(require_empty_request_body)],
)
def cancel(
    reference: str,
    db: Session = Depends(get_db),
    current_customer: AuthenticatedCustomer = Depends(get_current_customer),
):
    canonical_reference = _canonical_request_reference(reference)
    result = service.cancel_reservation_by_reference(
        db,
        canonical_reference,
        owner_customer_id=current_customer.id,
    )
    return map_public_reservation(_require_reservation(result))
