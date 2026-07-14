from sqlalchemy.orm import Session

from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate


class ReservationService:

    def __init__(self):
        self.repository = ReservationRepository()

    def create_reservation(
        self,
        db: Session,
        data: ReservationCreate,
    ):
        return self.repository.create(db, data)