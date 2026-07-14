from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.schemas.reservation import ReservationCreate
from app.services.reservation.service import ReservationService

router = APIRouter(prefix="/reservation", tags=["Reservation"])

service = ReservationService()


@router.post("/")
def create(
    reservation: ReservationCreate,
    db: Session = Depends(get_db),
):
    return service.create_reservation(db, reservation)