from collections.abc import Callable
from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.core.ownership import require_owner_customer_id
from app.core.input_validation import (
    validate_reservation_date,
    validate_reservation_field,
)
from app.core.transaction_errors import PersistenceOperationError
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate
from app.core.unit_of_work import UnitOfWork
from app.services.reservation.dto import (
    PersistedReservationDTO,
    ReservationSelectionPage,
)
from app.services.reservation.errors import (
    PastReservationDateError,
    PastReservationTimeError,
)
from app.services.reservation.public_reference import (
    require_canonical_public_reference,
)
from app.utils.datetime_parser import (
    current_local_date,
    current_local_datetime,
)


class ReservationService:

    CREATE_FIELDS = ("name", "people", "date", "time")

    def __init__(
        self,
        repository=None,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository or ReservationRepository()
        self.clock = clock

    @staticmethod
    def _dto(value) -> PersistedReservationDTO | None:
        if value is None:
            return None
        persisted_reference = vars(value).get("public_reference")
        reference = require_canonical_public_reference(persisted_reference)
        return PersistedReservationDTO(
            id=value.id,
            name=value.name,
            people=value.people,
            date=value.date,
            time=value.time,
            status=value.status,
            reference=reference,
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
        *,
        before_mutation: Callable[[], None] | None = None,
    ):
        require_owner_customer_id(owner_customer_id)
        validated_data = self._fresh_create_data(data)
        self.validate_new_reservation_datetime(
            validated_data.date,
            validated_data.time,
        )
        if before_mutation is not None:
            before_mutation()
        with UnitOfWork(db) as unit:
            persisted = self.repository.create(
                db,
                validated_data,
                owner_customer_id=owner_customer_id,
            )
            result = self._dto(persisted)
            unit.commit()
        return result

    def validate_new_reservation_date(self, value: str) -> str:
        canonical = validate_reservation_date(value)
        if date.fromisoformat(canonical) < current_local_date(clock=self.clock):
            raise PastReservationDateError()
        return canonical

    def validate_new_reservation_datetime(
        self,
        date_value: str,
        time_value: str,
    ) -> tuple[str, str]:
        canonical_date = validate_reservation_date(date_value)
        canonical_time = validate_reservation_field("time", time_value)
        requested_date = date.fromisoformat(canonical_date)
        requested_time = time.fromisoformat(canonical_time)
        now = current_local_datetime(clock=self.clock)
        if requested_date < now.date():
            raise PastReservationDateError()
        current_minute = now.time().replace(
            second=0,
            microsecond=0,
            tzinfo=None,
        )
        if requested_date == now.date() and requested_time < current_minute:
            raise PastReservationTimeError()
        return canonical_date, canonical_time

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

    def list_selectable_reservations(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 5,
    ):
        return self.list_selectable_reservation_page(
            db,
            owner_customer_id=owner_customer_id,
            after_public_reference=None,
            page_size=limit,
        ).reservations

    def list_selectable_reservation_page(
        self,
        db: Session,
        owner_customer_id,
        *,
        after_public_reference: str | None = None,
        page_size: int = 5,
    ) -> ReservationSelectionPage:
        require_owner_customer_id(owner_customer_id)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 5
        ):
            raise ValueError("Reservation page size must be between 1 and 5.")
        with UnitOfWork(db) as unit:
            list_page = getattr(self.repository, "list_active_page", None)
            if list_page is None:
                if after_public_reference is not None:
                    rows = ()
                else:
                    list_active = getattr(
                        self.repository,
                        "list_active_recent",
                        None,
                    )
                    if list_active is not None:
                        rows = list_active(
                            db,
                            owner_customer_id=owner_customer_id,
                            limit=page_size + 1,
                        )
                    else:
                        rows = tuple(
                            row
                            for row in self.repository.list_recent(
                                db,
                                owner_customer_id=owner_customer_id,
                                limit=page_size + 1,
                            )
                            if str(getattr(row, "status", "")).lower()
                            != "cancelled"
                        )
            else:
                rows = list_page(
                    db,
                    owner_customer_id=owner_customer_id,
                    after_public_reference=after_public_reference,
                    limit=page_size + 1,
                )
            has_more = len(rows) > page_size
            result = ReservationSelectionPage(
                reservations=tuple(
                    self._dto(row) for row in rows[:page_size]
                ),
                has_more=has_more,
            )
            unit.commit()
        return result

    def list_owner_reservations(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 50,
    ) -> tuple[tuple[PersistedReservationDTO, ...], int]:
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            rows = self.repository.list_for_owner(
                db,
                owner_customer_id=owner_customer_id,
                limit=limit,
            )
            count = self.repository.count_for_owner(
                db,
                owner_customer_id=owner_customer_id,
            )
            result = tuple(self._dto(row) for row in rows)
            unit.commit()
        return result, count

    def get_reservation_by_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            row = self.repository.get_by_public_reference(
                db,
                public_reference,
                owner_customer_id,
            )
            result = self._dto(row)
            unit.commit()
        return result

    def get_selectable_reservation_by_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            get_active = getattr(
                self.repository,
                "get_active_by_public_reference",
                None,
            )
            if get_active is None:
                row = self.repository.get_by_public_reference(
                    db,
                    public_reference,
                    owner_customer_id,
                )
                if str(getattr(row, "status", "")).lower() == "cancelled":
                    row = None
            else:
                row = get_active(
                    db,
                    public_reference,
                    owner_customer_id,
                )
            result = self._dto(row)
            unit.commit()
        return result

    def update_reservation_field_by_reference(
        self,
        db: Session,
        public_reference: str,
        field_name: str,
        new_value,
        owner_customer_id,
        *,
        before_mutation: Callable[[], None] | None = None,
    ):
        require_owner_customer_id(owner_customer_id)
        new_value = validate_reservation_field(field_name, new_value)
        try:
            with UnitOfWork(db) as unit:
                current = None
                if field_name in {"date", "time"}:
                    current = self.repository.get_active_by_public_reference(
                        db,
                        public_reference,
                        owner_customer_id,
                    )
                    if current is not None:
                        candidate_date = (
                            new_value if field_name == "date" else current.date
                        )
                        candidate_time = (
                            new_value if field_name == "time" else current.time
                        )
                        self.validate_new_reservation_datetime(
                            candidate_date,
                            candidate_time,
                        )

                if field_name in {"date", "time"} and current is None:
                    result = None
                else:
                    if before_mutation is not None:
                        before_mutation()
                    persisted = (
                        self.repository.update_reservation_field_by_public_reference(
                            db,
                            public_reference,
                            field_name,
                            new_value,
                            owner_customer_id,
                        )
                    )
                    result = self._dto(persisted)
                unit.commit()
            return result
        except PersistenceOperationError as error:
            if isinstance(
                error.__cause__,
                (PastReservationDateError, PastReservationTimeError),
            ):
                raise error.__cause__ from None
            raise

    def cancel_reservation_by_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        with UnitOfWork(db) as unit:
            persisted = (
                self.repository.cancel_reservation_by_public_reference(
                    db,
                    public_reference,
                    owner_customer_id,
                )
            )
            result = self._dto(persisted)
            unit.commit()
        return result
