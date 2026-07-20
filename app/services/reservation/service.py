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
        customer_id: str | None = None,
        owner_customer_id=None,
    ):
        return self.repository.create(
            db,
            data,
            customer_id=customer_id,
            owner_customer_id=owner_customer_id,
        )

    def list_recent_reservations(
        self,
        db: Session,
        customer_id: str,
        limit: int = 5,
    ):
        return self.repository.list_recent(
            db,
            customer_id=customer_id,
            limit=limit,
        )

    def get_reservation_by_id(
        self,
        db: Session,
        reservation_id: int,
        customer_id: str,
    ):
        return self.repository.get_by_id(db, reservation_id, customer_id)

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
        customer_id: str,
    ):
        return self.repository.update_reservation_field(
            db,
            reservation_id,
            field_name,
            new_value,
            customer_id,
        )

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        customer_id: str,
    ):
        return self.repository.cancel_reservation(
            db,
            reservation_id,
            customer_id,
        )
