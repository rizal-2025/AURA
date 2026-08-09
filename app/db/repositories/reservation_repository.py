import sqlite3

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ownership import require_owner_customer_id
from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate
from app.services.reservation.public_reference import (
    PUBLIC_REFERENCE_MAX_ATTEMPTS,
    PUBLIC_REFERENCE_UNIQUE_CONSTRAINT,
    PublicReservationReferenceCollisionError,
    canonicalize_public_reference,
    generate_public_reference,
)


def _is_public_reference_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    if (
        getattr(diagnostic, "constraint_name", None)
        == PUBLIC_REFERENCE_UNIQUE_CONSTRAINT
    ):
        return True

    return (
        isinstance(original, sqlite3.IntegrityError)
        and getattr(original, "sqlite_errorcode", None)
        == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and str(original)
        == "UNIQUE constraint failed: reservations.public_reference"
    )


class ReservationRepository:

    EDITABLE_FIELDS = {"name", "people", "date", "time"}

    def list_for_owner(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 50,
    ):
        require_owner_customer_id(owner_customer_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Reservation list limit must be between 1 and 50.")
        return list(
            db.execute(
                select(Reservation)
                .where(Reservation.owner_customer_id == owner_customer_id)
                .order_by(
                    Reservation.date.asc(),
                    Reservation.time.asc(),
                    Reservation.id.asc(),
                )
                .limit(limit)
            ).scalars()
        )

    def count_for_owner(self, db: Session, owner_customer_id) -> int:
        require_owner_customer_id(owner_customer_id)
        return int(
            db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.owner_customer_id == owner_customer_id)
            )
            or 0
        )

    def delete_by_owner_customer_id(
        self,
        db: Session,
        owner_customer_id,
    ) -> int:
        require_owner_customer_id(owner_customer_id)
        result = db.execute(
            delete(Reservation).where(
                Reservation.owner_customer_id == owner_customer_id
            )
        )
        return int(result.rowcount or 0)

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

    def list_active_recent(
        self,
        db: Session,
        owner_customer_id,
        limit: int = 5,
    ):
        return self.list_active_page(
            db,
            owner_customer_id=owner_customer_id,
            after_public_reference=None,
            limit=limit,
        )

    def list_active_page(
        self,
        db: Session,
        owner_customer_id,
        after_public_reference: str | None,
        limit: int = 5,
    ):
        require_owner_customer_id(owner_customer_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Reservation list limit must be between 1 and 50.")
        query = db.query(Reservation).filter(
            Reservation.owner_customer_id == owner_customer_id,
            func.lower(Reservation.status) != "cancelled",
        )
        if after_public_reference is not None:
            canonical_cursor = canonicalize_public_reference(
                after_public_reference
            )
            cursor_id = (
                db.query(Reservation.id)
                .filter(
                    Reservation.owner_customer_id == owner_customer_id,
                    Reservation.public_reference == canonical_cursor,
                )
                .scalar_subquery()
            )
            query = query.filter(Reservation.id < cursor_id)
        return (
            query
            .order_by(Reservation.id.desc())
            .limit(limit)
            .all()
        )

    def get_by_id_for_workflow_v1_conversion(
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

    def get_by_public_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        canonical_reference = canonicalize_public_reference(public_reference)
        return (
            db.query(Reservation)
            .filter(
                Reservation.owner_customer_id == owner_customer_id,
                Reservation.public_reference == canonical_reference,
            )
            .first()
        )

    def get_active_by_public_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        canonical_reference = canonicalize_public_reference(public_reference)
        return (
            db.query(Reservation)
            .filter(
                Reservation.owner_customer_id == owner_customer_id,
                Reservation.public_reference == canonical_reference,
                func.lower(Reservation.status) != "cancelled",
            )
            .first()
        )

    def update_reservation_field_by_public_reference(
        self,
        db: Session,
        public_reference: str,
        field_name: str,
        new_value,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        canonical_reference = canonicalize_public_reference(public_reference)
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"Field '{field_name}' cannot be updated.")

        statement = (
            update(Reservation)
            .where(
                Reservation.owner_customer_id == owner_customer_id,
                Reservation.public_reference == canonical_reference,
                func.lower(Reservation.status) != "cancelled",
            )
            .values({field_name: new_value})
            .returning(Reservation)
        )
        return db.execute(statement).scalar_one_or_none()

    def cancel_reservation_by_public_reference(
        self,
        db: Session,
        public_reference: str,
        owner_customer_id,
    ):
        require_owner_customer_id(owner_customer_id)
        canonical_reference = canonicalize_public_reference(public_reference)
        statement = (
            update(Reservation)
            .where(
                Reservation.owner_customer_id == owner_customer_id,
                Reservation.public_reference == canonical_reference,
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

        for _attempt in range(PUBLIC_REFERENCE_MAX_ATTEMPTS):
            data = Reservation(
                **reservation_fields,
                public_reference=generate_public_reference(),
            )
            try:
                with db.begin_nested():
                    db.add(data)
                    db.flush()
                return data
            except IntegrityError as error:
                if not _is_public_reference_unique_violation(error):
                    raise

        raise PublicReservationReferenceCollisionError()
