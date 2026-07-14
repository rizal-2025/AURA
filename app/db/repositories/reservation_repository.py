from sqlalchemy.orm import Session

from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate


class ReservationRepository:

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