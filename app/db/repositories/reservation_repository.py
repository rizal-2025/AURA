from sqlalchemy.orm import Session

from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate


class ReservationRepository:

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
