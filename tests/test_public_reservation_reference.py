import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.db.repositories.reservation_repository import (
    ReservationRepository,
    _is_public_reference_unique_violation,
)
from app.schemas.reservation import ReservationCreate
from app.services.reservation.public_reference import (
    PUBLIC_REFERENCE_LENGTH,
    PUBLIC_REFERENCE_MAX_ATTEMPTS,
    InvalidPublicReservationReferenceError,
    PublicReservationReferenceCollisionError,
    PublicReservationReferenceUnavailableError,
    canonicalize_public_reference,
    generate_public_reference,
    is_valid_public_reference,
)
from app.services.reservation.service import ReservationService


REFERENCE_A = "RSV_" + ("a" * 32)
REFERENCE_B = "RSV_" + ("b" * 32)
FROZEN_NOW = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def frozen_clock():
    return FROZEN_NOW


def reservation_data(**overrides):
    values = {
        "name": "Rizal",
        "people": 4,
        "date": "2026-08-01",
        "time": "19:00",
    }
    values.update(overrides)
    return ReservationCreate(**values)


class TestPublicReservationReferenceUtility(unittest.TestCase):
    def test_generator_uses_exact_contract(self):
        with patch(
            "app.services.reservation.public_reference.secrets.token_hex",
            return_value="1a" * 16,
        ) as token_hex:
            reference = generate_public_reference()

        token_hex.assert_called_once_with(16)
        self.assertEqual(reference, "RSV_" + ("1a" * 16))
        self.assertEqual(len(reference), PUBLIC_REFERENCE_LENGTH)
        self.assertRegex(reference, r"^RSV_[0-9a-f]{32}$")

    def test_two_generated_references_use_independent_random_calls(self):
        with patch(
            "app.services.reservation.public_reference.secrets.token_hex",
            side_effect=["1" * 32, "2" * 32],
        ):
            first = generate_public_reference()
            second = generate_public_reference()
        self.assertNotEqual(first, second)

    def test_canonicalizer_accepts_canonical_and_mixed_case(self):
        self.assertEqual(
            canonicalize_public_reference(REFERENCE_A),
            REFERENCE_A,
        )
        self.assertEqual(
            canonicalize_public_reference("rSv_" + ("A" * 32)),
            REFERENCE_A,
        )

    def test_validator_accepts_complete_reference_only(self):
        self.assertTrue(is_valid_public_reference(REFERENCE_A))
        self.assertTrue(is_valid_public_reference("rsv_" + ("A" * 32)))
        for value in (
            " " + REFERENCE_A,
            REFERENCE_A + " ",
            REFERENCE_A + "\n",
            REFERENCE_A + "\r",
            REFERENCE_A + "\r\n",
            "\n" + REFERENCE_A,
            "RSV_" + ("a" * 15) + "\n" + ("a" * 16),
            "RSV_" + ("a" * 15) + "\t" + ("a" * 16),
            "RSV_" + ("a" * 15) + " " + ("a" * 16),
            "RSV_" + ("a" * 31),
            "RSV_" + ("a" * 33),
            "RES_" + ("a" * 32),
            "RSV_" + ("g" * 32),
            "RSV__" + ("a" * 32),
            17,
            None,
        ):
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(is_valid_public_reference(value))
                with self.assertRaises(
                    InvalidPublicReservationReferenceError
                ) as captured:
                    canonicalize_public_reference(value)
                self.assertNotIn(str(value), str(captured.exception))
                self.assertEqual(
                    str(captured.exception),
                    "INVALID_RESERVATION_REFERENCE",
                )


class TestPublicReservationReferenceFoundation(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Customer.__table__.create(self.engine)
        Reservation.__table__.create(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.owner_a = uuid4()
        self.owner_b = uuid4()
        with self.Session.begin() as db:
            db.add_all(
                [
                    Customer(id=self.owner_a),
                    Customer(id=self.owner_b),
                ]
            )

    def tearDown(self):
        self.engine.dispose()

    def _create(self, owner=None):
        with self.Session() as db:
            service = ReservationService(clock=frozen_clock)
            return service.create_reservation(
                db,
                reservation_data(),
                owner_customer_id=owner or self.owner_a,
            )

    def test_model_column_is_nullable_unique_and_sqlite_compatible(self):
        column = Reservation.__table__.c.public_reference
        self.assertTrue(column.nullable)
        self.assertEqual(column.type.length, 36)
        constraint_names = {
            constraint.name
            for constraint in Reservation.__table__.constraints
        }
        self.assertIn(
            "uq_reservations_public_reference",
            constraint_names,
        )

    def test_supported_create_generates_reference_without_public_injection(self):
        created = self._create()
        self.assertIsNotNone(created.reference)
        self.assertEqual(
            created.reference,
            canonicalize_public_reference(created.reference),
        )

        with self.assertRaises(ValidationError):
            ReservationCreate.model_validate(
                {
                    "name": "Rizal",
                    "people": 4,
                    "date": "2026-08-01",
                    "time": "19:00",
                    "public_reference": REFERENCE_A,
                }
            )

    def test_reference_lookup_is_owner_scoped_and_canonicalized(self):
        created = self._create()
        service = ReservationService(clock=frozen_clock)
        mixed_case = created.reference.upper()

        with self.Session() as db:
            owned = service.get_reservation_by_reference(
                db,
                mixed_case,
                self.owner_a,
            )
        with self.Session() as db:
            foreign = service.get_reservation_by_reference(
                db,
                mixed_case,
                self.owner_b,
            )
        with self.Session() as db:
            missing = service.get_reservation_by_reference(
                db,
                REFERENCE_B,
                self.owner_b,
            )

        self.assertEqual(owned.id, created.id)
        self.assertEqual(owned.reference, created.reference)
        self.assertIsNone(foreign)
        self.assertIsNone(missing)

    def test_malformed_reference_is_rejected_before_query(self):
        repository = ReservationRepository()
        db = MagicMock()

        for operation in (
            lambda: repository.get_by_public_reference(
                db,
                " 17 ",
                self.owner_a,
            ),
            lambda: repository.update_reservation_field_by_public_reference(
                db,
                "17",
                "people",
                5,
                self.owner_a,
            ),
            lambda: repository.cancel_reservation_by_public_reference(
                db,
                "RSV_invalid",
                self.owner_a,
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(
                    InvalidPublicReservationReferenceError
                ):
                    operation()

        db.query.assert_not_called()
        db.execute.assert_not_called()

    def test_reference_is_immutable_across_reference_update_and_cancel(self):
        created = self._create()
        service = ReservationService(clock=frozen_clock)

        with self.Session() as db:
            updated = service.update_reservation_field_by_reference(
                db,
                created.reference,
                "people",
                6,
                self.owner_a,
            )
        with self.Session() as db:
            cancelled = service.cancel_reservation_by_reference(
                db,
                created.reference,
                self.owner_a,
            )

        self.assertEqual(updated.people, 6)
        self.assertEqual(updated.reference, created.reference)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.reference, created.reference)
        with self.Session() as db:
            persisted = db.get(Reservation, created.id)
            self.assertEqual(
                persisted.public_reference,
                created.reference,
            )

    def test_numeric_lookup_is_converter_only_and_existing_list_order_remains(self):
        first = self._create()
        second = self._create()
        service = ReservationService(clock=frozen_clock)

        with self.Session() as db:
            numeric = ReservationRepository().get_by_id_for_workflow_v1_conversion(
                db,
                first.id,
                self.owner_a,
            )
        with self.Session() as db:
            listed = service.list_recent_reservations(
                db,
                self.owner_a,
                limit=5,
            )

        self.assertEqual(numeric.id, first.id)
        self.assertEqual(
            [item.id for item in listed],
            [second.id, first.id],
        )

    def test_legacy_null_reference_fails_closed_at_safe_service_boundary(self):
        with self.Session.begin() as db:
            row = Reservation(
                name="Legacy",
                people=2,
                date="2026-08-02",
                time="20:00",
                owner_customer_id=self.owner_a,
                public_reference=None,
            )
            db.add(row)

        with self.Session() as db:
            with self.assertRaises(PublicReservationReferenceUnavailableError):
                ReservationService(
                    clock=frozen_clock
                ).list_recent_reservations(
                    db,
                    self.owner_a,
                )

    def test_exact_collision_uses_savepoint_and_retries(self):
        with self.Session.begin() as db:
            db.add(
                Reservation(
                    name="Existing",
                    people=2,
                    date="2026-08-02",
                    time="20:00",
                    owner_customer_id=self.owner_a,
                    public_reference=REFERENCE_A,
                )
            )

        with patch(
            "app.db.repositories.reservation_repository."
            "generate_public_reference",
            side_effect=[REFERENCE_A, REFERENCE_B],
        ) as generator:
            created = self._create(owner=self.owner_b)

        self.assertEqual(created.reference, REFERENCE_B)
        self.assertEqual(generator.call_count, 2)
        with self.Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(Reservation)
                ),
                2,
            )

    def test_savepoint_retry_preserves_unrelated_pending_state(self):
        with self.Session.begin() as db:
            db.add(
                Reservation(
                    name="Existing",
                    people=2,
                    date="2026-08-02",
                    time="20:00",
                    owner_customer_id=self.owner_a,
                    public_reference=REFERENCE_A,
                )
            )

        unrelated = Customer(id=uuid4())
        with self.Session() as db:
            db.add(unrelated)
            with patch.object(
                db,
                "commit",
                wraps=db.commit,
            ) as commit, patch.object(
                db,
                "rollback",
                wraps=db.rollback,
            ) as rollback, patch(
                "app.db.repositories.reservation_repository."
                "generate_public_reference",
                side_effect=[REFERENCE_A, REFERENCE_B],
            ) as generator:
                created = ReservationRepository().create(
                    db,
                    reservation_data(),
                    self.owner_b,
                )

                self.assertEqual(generator.call_count, 2)
                self.assertTrue(sa_inspect(unrelated).persistent)
                self.assertTrue(sa_inspect(created).persistent)
                self.assertFalse(sa_inspect(created).pending)
                self.assertEqual(
                    [
                        value
                        for value in db.identity_map.values()
                        if isinstance(value, Reservation)
                    ],
                    [created],
                )
                self.assertEqual(
                    db.scalar(
                        select(func.count()).select_from(Reservation)
                    ),
                    2,
                )
                commit.assert_not_called()
                rollback.assert_not_called()

    def test_repository_exhaustion_leaves_no_failed_pending_candidate(self):
        with self.Session.begin() as db:
            db.add(
                Reservation(
                    name="Existing",
                    people=2,
                    date="2026-08-02",
                    time="20:00",
                    owner_customer_id=self.owner_a,
                    public_reference=REFERENCE_A,
                )
            )

        with self.Session() as db, patch.object(
            db,
            "commit",
            wraps=db.commit,
        ) as commit, patch.object(
            db,
            "rollback",
            wraps=db.rollback,
        ) as rollback, patch(
            "app.db.repositories.reservation_repository."
            "generate_public_reference",
            return_value=REFERENCE_A,
        ) as generator:
            with self.assertRaises(
                PublicReservationReferenceCollisionError
            ):
                ReservationRepository().create(
                    db,
                    reservation_data(),
                    self.owner_b,
                )

            self.assertEqual(
                generator.call_count,
                PUBLIC_REFERENCE_MAX_ATTEMPTS,
            )
            self.assertFalse(
                any(isinstance(value, Reservation) for value in db.new)
            )
            self.assertFalse(
                any(
                    isinstance(value, Reservation)
                    for value in db.identity_map.values()
                )
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Reservation)),
                1,
            )
            self.assertTrue(db.is_active)
            commit.assert_not_called()
            rollback.assert_not_called()

    def test_collision_exhaustion_is_bounded_and_session_recovers(self):
        with self.Session.begin() as db:
            db.add(
                Reservation(
                    name="Existing",
                    people=2,
                    date="2026-08-02",
                    time="20:00",
                    owner_customer_id=self.owner_a,
                    public_reference=REFERENCE_A,
                )
            )

        with self.Session() as db, patch(
            "app.db.repositories.reservation_repository."
            "generate_public_reference",
            return_value=REFERENCE_A,
        ) as generator:
            with self.assertRaises(
                PublicReservationReferenceCollisionError
            ) as captured:
                ReservationService(clock=frozen_clock).create_reservation(
                    db,
                    reservation_data(),
                    self.owner_b,
                )
            self.assertEqual(
                str(captured.exception),
                "RESERVATION_REFERENCE_UNAVAILABLE",
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Customer)),
                2,
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Reservation)),
                1,
            )
            self.assertFalse(
                any(isinstance(value, Reservation) for value in db.new)
            )

        self.assertEqual(
            generator.call_count,
            PUBLIC_REFERENCE_MAX_ATTEMPTS,
        )

    def test_unrelated_integrity_error_is_not_retried(self):
        db = MagicMock()
        unrelated = IntegrityError(
            "private statement",
            {},
            RuntimeError("private database detail"),
        )
        db.flush.side_effect = unrelated

        with patch(
            "app.db.repositories.reservation_repository."
            "generate_public_reference",
            return_value=REFERENCE_A,
        ) as generator:
            with self.assertRaises(IntegrityError) as captured:
                ReservationRepository().create(
                    db,
                    reservation_data(),
                    self.owner_a,
                )

        self.assertIs(captured.exception, unrelated)
        self.assertEqual(generator.call_count, 1)

    def test_collision_detection_uses_exact_constraint_metadata(self):
        class Diagnostic:
            constraint_name = "uq_reservations_public_reference"

        class OriginalError(Exception):
            diag = Diagnostic()

        exact = IntegrityError("", {}, OriginalError())
        self.assertTrue(_is_public_reference_unique_violation(exact))

        Diagnostic.constraint_name = "uq_reservations_other"
        unrelated = IntegrityError("", {}, OriginalError())
        self.assertFalse(_is_public_reference_unique_violation(unrelated))

        sqlite_original = sqlite3.IntegrityError(
            "UNIQUE constraint failed: reservations.public_reference"
        )
        sqlite_original.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        sqlite_error = IntegrityError("", {}, sqlite_original)
        self.assertTrue(
            _is_public_reference_unique_violation(sqlite_error)
        )


if __name__ == "__main__":
    unittest.main()
