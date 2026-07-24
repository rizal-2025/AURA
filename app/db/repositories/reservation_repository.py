from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.ownership import require_owner_customer_id
from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate


class ReservationRepository:

    EDITABLE_FIELDS = {"name", "people", "date", "time"}

    def list_recent(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 5,
    ):
        require_owner_customer_id(owner_customer_id)
        return (
            db.query(Reservation)
            .filter(Reservation.owner_customer_id == owner_customer_id)
            .order_by(Reservation.id.desc())
            .limit(limit)
            .all()
        )

    def get_by_id(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        return (
            db.query(Reservation)
            .filter(Reservation.id == reservation_id)
            .filter(Reservation.owner_customer_id == owner_customer_id)
            .first()
        )

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"Field '{field_name}' cannot be updated.")

        statement = (
            update(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.owner_customer_id == owner_customer_id,
                func.lower(Reservation.status) != "cancelled",
            )
            .values({field_name: new_value})
            .returning(Reservation)
        )
        return db.execute(statement).scalar_one_or_none()

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        statement = (
            update(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.owner_customer_id == owner_customer_id,
                func.lower(Reservation.status) != "cancelled",
            )
            .values(status="cancelled")
            .returning(Reservation)
        )
        return db.execute(statement).scalar_one_or_none()

    def create(
        self,
        db: Session,
        reservation: ReservationCreate,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)

        reservation_fields = {
            "name": reservation.name,
            "people": reservation.people,
            "date": reservation.date,
            "time": reservation.time,
        }
        reservation_fields["owner_customer_id"] = owner_customer_id

        data = Reservation(
            **reservation_fields,
        )

        db.add(data)
        db.flush()

        return data
