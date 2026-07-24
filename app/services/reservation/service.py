from sqlalchemy.orm import Session

from app.core.ownership import require_owner_customer_id
from app.core.input_validation import validate_reservation_field
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate


class ReservationService:

    CREATE_FIELDS = ("name", "people", "date", "time")

    def __init__(self):
        self.repository = ReservationRepository()

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
        return self.repository.create(
            db,
            validated_data,
            owner_customer_id=owner_customer_id,
        )

    def list_recent_reservations(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 5,
    ):
        require_owner_customer_id(owner_customer_id)
        return self.repository.list_recent(
            db,
            owner_customer_id=owner_customer_id,
            limit=limit,
        )

    def get_reservation_by_id(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        return self.repository.get_by_id(db, reservation_id, owner_customer_id)

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
        return self.repository.update_reservation_field(
            db,
            reservation_id,
            field_name,
            new_value,
            owner_customer_id,
        )

    def cancel_reservation(
        self,
        db: Session,
        reservation_id: int,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        return self.repository.cancel_reservation(
            db,
            reservation_id,
            owner_customer_id,
        )
