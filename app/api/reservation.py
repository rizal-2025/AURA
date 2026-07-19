from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.schemas.reservation import ReservationCreate, ReservationResponse
from app.services.reservation.service import ReservationService

router = APIRouter(prefix="/reservation", tags=["Reservation"])

service = ReservationService()


@router.post("/", response_model=ReservationResponse)
def create(
    reservation: ReservationCreate,
    db: Session = Depends(get_db),
    session_id: str = Header(alias="X-Session-ID"),
):
    return service.create_reservation(
        db,
        reservation,
        customer_id=session_id,
    )
