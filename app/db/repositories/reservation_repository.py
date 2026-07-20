from sqlalchemy.orm import Session

from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate


class ReservationRepository:

    EDITABLE_FIELDS = {"name", "people", "date", "time"}

    def list_recent(
        self,
        db: Session,
        customer_id: str,
        limit: int = 5,
    ):
        return (
            db.query(Reservation)
            .filter(Reservation.customer_id == customer_id)
            .order_by(Reservation.id.desc())
            .limit(limit)
            .all()
        )

    def get_by_id(
        self,
        db: Session,
        reservation_id: int,
        customer_id: str,
    ):
        return (
            db.query(Reservation)
            .filter(Reservation.id == reservation_id)
            .filter(Reservation.customer_id == customer_id)
            .first()
        )

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
        customer_id: str,
    ):
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"Field '{field_name}' cannot be updated.")

        reservation = self.get_by_id(db, reservation_id, customer_id)
        if reservation is None:
            return None

        setattr(reservation, field_name, new_value)
        db.commit()
        db.refresh(reservation)
        return reservation

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        customer_id: str,
    ):
        reservation = self.get_by_id(db, reservation_id, customer_id)
        if reservation is None or str(reservation.status).lower() == "cancelled":
            return None

        reservation.status = "cancelled"
        db.commit()
        db.refresh(reservation)
        return reservation

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
