from sqlalchemy import func, update
from sqlalchemy.orm import Session

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
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"Field '{field_name}' cannot be updated.")

        statement = (
            update(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.owner_customer_id == owner_customer_id,
            )
            .values({field_name: new_value})
        )
        result = db.execute(statement)
        if result.rowcount != 1:
            return None

        db.commit()
        return self.get_by_id(db, reservation_id, owner_customer_id)

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        statement = (
            update(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.owner_customer_id == owner_customer_id,
                func.lower(Reservation.status) != "cancelled",
            )
            .values(status="cancelled")
        )
        result = db.execute(statement)
        if result.rowcount != 1:
            return None

        db.commit()
        return self.get_by_id(db, reservation_id, owner_customer_id)

    def create(
        self,
        db: Session,
        reservation: ReservationCreate,
        customer_id: str | None = None,
        owner_customer_id=None,
    ):
        if not customer_id and owner_customer_id is None:
            raise ValueError(
                "customer_id or owner_customer_id is required when creating a reservation."
            )

        reservation_fields = {
            "name": reservation.name,
            "people": reservation.people,
            "date": reservation.date,
            "time": reservation.time,
        }
        if customer_id is not None:
            reservation_fields["customer_id"] = customer_id
        if owner_customer_id is not None:
            reservation_fields["owner_customer_id"] = owner_customer_id

        data = Reservation(
            **reservation_fields,
        )

        db.add(data)
        db.commit()
        db.refresh(data)

        return data
