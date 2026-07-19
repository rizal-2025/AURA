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

    def list_recent_reservations(
        self,
        db: Session,
        limit: int = 5,
    ):
        return self.repository.list_recent(db, limit=limit)

    def get_reservation_by_id(
        self,
        db: Session,
        reservation_id: int,
    ):
        return self.repository.get_by_id(db, reservation_id)

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
    ):
        return self.repository.update_reservation_field(
            db,
            reservation_id,
            field_name,
            new_value,
        )
