from sqlalchemy.orm import Session

from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate


class ReservationRepository:

    EDITABLE_FIELDS = {"name", "people", "date", "time"}

    def list_recent(
        self,
        db: Session,
        limit: int = 5,
    ):
        return (
            db.query(Reservation)
            .order_by(Reservation.id.desc())
            .limit(limit)
            .all()
        )

    def get_by_id(
        self,
        db: Session,
        reservation_id: int,
    ):
        return (
            db.query(Reservation)
            .filter(Reservation.id == reservation_id)
            .first()
        )

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
    ):
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"Field '{field_name}' cannot be updated.")

        reservation = self.get_by_id(db, reservation_id)
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
    ):
        reservation = self.get_by_id(db, reservation_id)
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
    ):

        data = Reservation(
            name=reservation.name,
            people=reservation.people,
            date=reservation.date,
            time=reservation.time,
        )

        db.add(data)
        db.commit()
        db.refresh(data)

        return data
