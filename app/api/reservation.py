from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_customer
from app.db.database import get_db
from app.db.models.customer import Customer
from app.schemas.reservation import ReservationCreate, ReservationResponse
from app.services.reservation.service import ReservationService

router = APIRouter(prefix="/reservation", tags=["Reservation"])

service = ReservationService()


@router.post("/", response_model=ReservationResponse)
def create(
    reservation: ReservationCreate,
    db: Session = Depends(get_db),
    current_customer: Customer = Depends(get_current_customer),
):
    return service.create_reservation(
        db,
        reservation,
        owner_customer_id=current_customer.id,
    )
