from sqlalchemy.orm import Session

from app.core.ownership import require_owner_customer_id
from app.core.input_validation import validate_reservation_field
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate
from app.core.unit_of_work import UnitOfWork
from app.services.reservation.dto import PersistedReservationDTO


class ReservationService:

    CREATE_FIELDS = ("name", "people", "date", "time")

    def __init__(self, repository=None):
        self.repository = repository or ReservationRepository()

    @staticmethod
    def _dto(value) -> PersistedReservationDTO | None:
        if value is None:
            return None
        return PersistedReservationDTO(
            id=value.id,
            name=value.name,
            people=value.people,
            date=value.date,
            time=value.time,
            status=value.status,
        )

    @classmethod
    def _fresh_create_data(cls, data) -> ReservationCreate:
        primitive_fields = {
            field_name: getattr(data, field_name, None)
            for field_name in cls.CREATE_FIELDS
        }
        return ReservationCreate.model_validate(primitive_fields)

    def create_reservation(
        self,
        db: Session,
        data: ReservationCreate,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        validated_data = self._fresh_create_data(data)
        with UnitOfWork(db) as unit:
            persisted = self.repository.create(
                db,
                validated_data,
                owner_customer_id=owner_customer_id,
            )
            result = self._dto(persisted)
            unit.commit()
        return result

    def list_recent_reservations(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 5,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            rows = self.repository.list_recent(
                db,
                owner_customer_id=owner_customer_id,
                limit=limit,
            )
            result = tuple(self._dto(row) for row in rows)
            unit.commit()
        return result

    def get_reservation_by_id(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            row = self.repository.get_by_id(
                db,
                reservation_id,
                owner_customer_id,
            )
            result = self._dto(row)
            unit.commit()
        return result

    def update_reservation_field(
        self,
        db: Session,
        reservation_id: int,
        field_name: str,
        new_value,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        new_value = validate_reservation_field(field_name, new_value)
        with UnitOfWork(db) as unit:
            persisted = self.repository.update_reservation_field(
                db,
                reservation_id,
                field_name,
                new_value,
                owner_customer_id,
            )
            result = self._dto(persisted)
            unit.commit()
        return result

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            persisted = self.repository.cancel_reservation(
                db,
                reservation_id,
                owner_customer_id,
            )
            result = self._dto(persisted)
            unit.commit()
        return result
