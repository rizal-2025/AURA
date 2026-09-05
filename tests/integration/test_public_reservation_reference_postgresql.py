"""PostgreSQL verification for the reservation public-reference migration."""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.transaction_errors import PersistenceOperationError
from app.db.models.customer import Customer  # noqa: F401
from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate
from app.services.reservation.public_reference import (
    PUBLIC_REFERENCE_MAX_ATTEMPTS,
    PublicReservationReferenceCollisionError,
)
from app.services.reservation.service import ReservationService
from migrations.add_public_reservation_reference import (
    FORMAT_CONSTRAINT,
    UNIQUE_CONSTRAINT,
    PublicReservationReferenceMigrationError,
    downgrade,
    migrate,
)
from tests.integration.disposable_schema import DisposableSchemaResources
from tests.integration.reservation_clock import install_reservation_clock


REFERENCE_A = "RSV_" + ("a" * 32)
REFERENCE_B = "RSV_" + ("b" * 32)
EXPECTED_FORMAT_DEFINITION = (
    "CHECK (public_reference IS NULL OR public_reference::text ~ "
    "'^RSV_[0-9a-f]{32}$'::text)"
)


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return (
            "TEST_DATABASE_URL is not configured; reservation reference "
            "PostgreSQL tests are skipped."
        )
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if parsed.database != "aura_test":
            return "TEST_DATABASE_URL must target the exact aura_test database."
    except Exception:
        return "TEST_DATABASE_URL is invalid; PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestPublicReservationReferenceMigrationPostgreSQL(
    unittest.TestCase
):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(
            os.environ["TEST_DATABASE_URL"],
            pool_pre_ping=True,
        )
        with cls.admin.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        if identity != ("aura_test", "aura_test_runner"):
            cls.admin.dispose()
            raise RuntimeError(
                "Dedicated PostgreSQL preflight identity did not match."
            )

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        install_reservation_clock(self)
        self.schema = f"aura_res_ref_test_{uuid4().hex[:12]}"
        self.resources = DisposableSchemaResources(
            admin_engine=self.admin,
            schema=self.schema,
            allowed_prefixes=("aura_res_ref_test_",),
            dispose_admin=False,
        )
        self.addCleanup(self.resources.cleanup)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))

        schema_url = make_url(
            os.environ["TEST_DATABASE_URL"]
        ).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(schema_url, pool_pre_ping=True)
        self.resources.track_engine(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE reservations ("
                    "id SERIAL PRIMARY KEY, "
                    "name VARCHAR(100) NOT NULL, "
                    "people INTEGER NOT NULL, "
                    "date VARCHAR(20) NOT NULL, "
                    "time VARCHAR(10) NOT NULL, "
                    "customer_id VARCHAR(255) NULL, "
                    "owner_customer_id UUID NULL, "
                    "status VARCHAR(20) NOT NULL DEFAULT 'pending'"
                    ")"
                )
            )

    def _insert(self, *, name: str, reference_marker=False):
        columns = "name, people, date, time, status"
        values = ":name, 4, '2026-08-01', '19:00', 'pending'"
        if reference_marker:
            columns += ", public_reference"
            values += ", :reference"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO reservations ({columns}) "
                    f"VALUES ({values})"
                ),
                {
                    "name": name,
                    "reference": (
                        REFERENCE_A if reference_marker else None
                    ),
                },
            )

    def _add_reference_column(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE reservations ADD COLUMN "
                    "public_reference VARCHAR(36) NULL"
                )
            )

    def _add_format_constraint(self, expression, *, name=FORMAT_CONSTRAINT):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f'ALTER TABLE reservations ADD CONSTRAINT "{name}" '
                    f"CHECK ({expression})"
                )
            )

    def _add_unique_constraint(self, *, name=UNIQUE_CONSTRAINT):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f'ALTER TABLE reservations ADD CONSTRAINT "{name}" '
                    "UNIQUE (public_reference)"
                )
            )

    def _constraint_definition(self, name):
        with self.engine.connect() as connection:
            return connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(c.oid, true) "
                    "FROM pg_constraint AS c "
                    "JOIN pg_class AS t ON t.oid = c.conrelid "
                    "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = :schema "
                    "AND t.relname = 'reservations' "
                    "AND c.conname = :name"
                ),
                {"schema": self.schema, "name": name},
            )

    def _assert_exact_owned_format(self):
        self.assertEqual(
            self._constraint_definition(FORMAT_CONSTRAINT),
            EXPECTED_FORMAT_DEFINITION,
        )

    def _assert_named_format_rejected(self, expression):
        self._add_reference_column()
        self._add_format_constraint(expression)
        original_definition = self._constraint_definition(FORMAT_CONSTRAINT)

        with self.assertRaises(
            PublicReservationReferenceMigrationError
        ) as captured:
            migrate(self.engine, schema=self.schema)

        self.assertEqual(
            str(captured.exception),
            "PUBLIC_RESERVATION_REFERENCE_MIGRATION_FAILED",
        )
        self.assertEqual(
            self._constraint_definition(FORMAT_CONSTRAINT),
            original_definition,
        )
        self.assertNotEqual(original_definition, EXPECTED_FORMAT_DEFINITION)

    @staticmethod
    def _reservation_insert_observer(target):
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("INSERT INTO RESERVATIONS"):
                target.append(statement)

        return observe

    def test_additive_backfill_constraints_and_idempotency(self):
        self._insert(name="Legacy")

        self.assertTrue(migrate(self.engine, schema=self.schema))
        inspector = inspect(self.engine)
        column = next(
            item
            for item in inspector.get_columns(
                "reservations",
                schema=self.schema,
            )
            if item["name"] == "public_reference"
        )
        self.assertTrue(column["nullable"])
        self.assertEqual(column["type"].length, 36)

        with self.engine.connect() as connection:
            reference = connection.scalar(
                text(
                    "SELECT public_reference FROM reservations "
                    "WHERE name = 'Legacy'"
                )
            )
        self.assertRegex(reference, r"^RSV_[0-9a-f]{32}$")
        self.assertFalse(migrate(self.engine, schema=self.schema))
        self._assert_exact_owned_format()

        inspector = inspect(self.engine)
        self.assertIn(
            FORMAT_CONSTRAINT,
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "reservations",
                    schema=self.schema,
                )
            },
        )
        self.assertIn(
            UNIQUE_CONSTRAINT,
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "reservations",
                    schema=self.schema,
                )
            },
        )

        self._insert(name="Temporary legacy null")
        with self.engine.connect() as connection:
            self.assertIsNone(
                connection.scalar(
                    text(
                        "SELECT public_reference FROM reservations "
                        "WHERE name = 'Temporary legacy null'"
                    )
                )
            )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO reservations "
                        "(name, people, date, time, status, "
                        "public_reference) VALUES "
                        "('Invalid', 2, '2026-08-01', '18:00', "
                        "'pending', 'RSV_INVALID')"
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO reservations "
                        "(name, people, date, time, status, "
                        "public_reference) VALUES "
                        "('Duplicate', 2, '2026-08-01', '18:00', "
                        "'pending', :reference)"
                    ),
                    {"reference": reference},
                )

    def test_exact_named_format_constraint_is_accepted(self):
        self._add_reference_column()
        self._add_format_constraint(
            "public_reference IS NULL OR public_reference "
            "~ '^RSV_[0-9a-f]{32}$'"
        )
        self._add_unique_constraint()

        self.assertFalse(migrate(self.engine, schema=self.schema))
        self._assert_exact_owned_format()

    def test_case_insensitive_named_format_constraint_is_rejected(self):
        self._assert_named_format_rejected(
            "public_reference IS NULL OR public_reference "
            "~* '^RSV_[0-9a-f]{32}$'"
        )

    def test_extra_or_named_format_constraint_is_rejected(self):
        self._assert_named_format_rejected(
            "public_reference IS NULL OR public_reference "
            "~ '^RSV_[0-9a-f]{32}$' OR status = 'pending'"
        )

    def test_wrong_column_named_format_constraint_is_rejected(self):
        self._assert_named_format_rejected(
            "status IS NULL OR status ~ '^RSV_[0-9a-f]{32}$'"
        )

    def test_wrong_regex_named_format_constraint_is_rejected(self):
        self._assert_named_format_rejected(
            "public_reference IS NULL OR public_reference "
            "~ '^RSV_[0-9a-f]{31}$'"
        )

    def test_unnamed_equivalent_does_not_replace_owned_constraint(self):
        legacy_name = "ck_reservations_public_reference_legacy"
        self._add_reference_column()
        self._add_format_constraint(
            "public_reference IS NULL OR public_reference "
            "~ '^RSV_[0-9a-f]{32}$'",
            name=legacy_name,
        )

        self.assertTrue(migrate(self.engine, schema=self.schema))
        checks = {
            item["name"]
            for item in inspect(self.engine).get_check_constraints(
                "reservations",
                schema=self.schema,
            )
        }
        self.assertIn(legacy_name, checks)
        self.assertIn(FORMAT_CONSTRAINT, checks)
        self._assert_exact_owned_format()

    def test_partial_schema_with_only_format_adds_named_unique(self):
        self._add_reference_column()
        self._add_format_constraint(
            "public_reference IS NULL OR public_reference "
            "~ '^RSV_[0-9a-f]{32}$'"
        )

        self.assertTrue(migrate(self.engine, schema=self.schema))
        self._assert_exact_owned_format()
        self.assertIn(
            UNIQUE_CONSTRAINT,
            {
                item["name"]
                for item in inspect(self.engine).get_unique_constraints(
                    "reservations",
                    schema=self.schema,
                )
            },
        )
        self.assertFalse(migrate(self.engine, schema=self.schema))

    def test_partial_schema_with_only_unique_adds_named_format(self):
        self._add_reference_column()
        self._add_unique_constraint()

        self.assertTrue(migrate(self.engine, schema=self.schema))
        self._assert_exact_owned_format()
        self.assertFalse(migrate(self.engine, schema=self.schema))

    def test_existing_valid_reference_is_preserved(self):
        self._add_reference_column()
        self._insert(name="Existing", reference_marker=True)
        self._insert(name="Needs backfill")

        with patch(
            "migrations.add_public_reservation_reference._reference",
            return_value=REFERENCE_B,
        ):
            self.assertTrue(migrate(self.engine, schema=self.schema))

        with self.engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text(
                        "SELECT name, public_reference FROM reservations"
                    )
                ).all()
            )
        self.assertEqual(rows["Existing"], REFERENCE_A)
        self.assertEqual(rows["Needs backfill"], REFERENCE_B)
        self.assertNotEqual(rows["Needs backfill"], "RSV_2")
        self._assert_exact_owned_format()

    def test_backfill_crosses_batch_boundary_and_rerun_preserves_values(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status) "
                    "SELECT 'Legacy-' || value, 4, '2026-08-01', "
                    "'19:00', 'pending' FROM generate_series(1, 501) AS value"
                )
            )

        self.assertTrue(migrate(self.engine, schema=self.schema))
        with self.engine.connect() as connection:
            total, populated, unique_count, canonical = connection.execute(
                text(
                    "SELECT count(*), count(public_reference), "
                    "count(DISTINCT public_reference), "
                    "bool_and(public_reference ~ '^RSV_[0-9a-f]{32}$') "
                    "FROM reservations"
                )
            ).one()
            before = tuple(
                connection.execute(
                    text(
                        "SELECT public_reference FROM reservations "
                        "ORDER BY id"
                    )
                ).scalars()
            )

        self.assertEqual((total, populated, unique_count), (501, 501, 501))
        self.assertTrue(canonical)
        self.assertFalse(migrate(self.engine, schema=self.schema))
        with self.engine.connect() as connection:
            after = tuple(
                connection.execute(
                    text(
                        "SELECT public_reference FROM reservations "
                        "ORDER BY id"
                    )
                ).scalars()
            )
        self.assertTrue(before == after)
        self._assert_exact_owned_format()

    def test_backfill_collision_is_bounded_and_rolls_back(self):
        self._add_reference_column()
        self._insert(name="Existing", reference_marker=True)
        self._insert(name="Needs backfill")

        with patch(
            "migrations.add_public_reservation_reference._reference",
            return_value=REFERENCE_A,
        ) as generator:
            with self.assertRaises(
                PublicReservationReferenceMigrationError
            ) as captured:
                migrate(self.engine, schema=self.schema)

        self.assertEqual(generator.call_count, 5)
        self.assertEqual(
            str(captured.exception),
            "PUBLIC_RESERVATION_REFERENCE_MIGRATION_FAILED",
        )
        with self.engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text(
                        "SELECT name, public_reference FROM reservations"
                    )
                ).all()
            )
        self.assertEqual(rows["Existing"], REFERENCE_A)
        self.assertIsNone(rows["Needs backfill"])
        self.assertNotIn(REFERENCE_A, str(captured.exception))

    def test_invalid_existing_reference_fails_closed(self):
        self._add_reference_column()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status, "
                    "public_reference) VALUES "
                    "('Invalid', 2, '2026-08-01', '18:00', "
                    "'pending', 'RSV_INVALID')"
                )
            )

        with self.assertRaises(
            PublicReservationReferenceMigrationError
        ) as captured:
            migrate(self.engine, schema=self.schema)
        self.assertEqual(
            str(captured.exception),
            "PUBLIC_RESERVATION_REFERENCE_MIGRATION_FAILED",
        )
        inspector = inspect(self.engine)
        self.assertNotIn(
            FORMAT_CONSTRAINT,
            {
                item["name"]
                for item in inspector.get_check_constraints(
                    "reservations",
                    schema=self.schema,
                )
            },
        )
        self.assertNotIn(
            UNIQUE_CONSTRAINT,
            {
                item["name"]
                for item in inspector.get_unique_constraints(
                    "reservations",
                    schema=self.schema,
                )
            },
        )

    def test_duplicate_existing_references_fail_and_roll_back(self):
        self._add_reference_column()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status, public_reference) "
                    "VALUES "
                    "('Duplicate A', 2, '2026-08-01', '18:00', "
                    "'pending', :reference), "
                    "('Duplicate B', 3, '2026-08-02', '19:00', "
                    "'pending', :reference)"
                ),
                {"reference": REFERENCE_A},
            )

        with self.assertRaises(
            PublicReservationReferenceMigrationError
        ) as captured:
            migrate(self.engine, schema=self.schema)

        self.assertEqual(
            str(captured.exception),
            "PUBLIC_RESERVATION_REFERENCE_MIGRATION_FAILED",
        )
        with self.engine.connect() as connection:
            count, distinct_count = connection.execute(
                text(
                    "SELECT count(*), count(DISTINCT public_reference) "
                    "FROM reservations"
                )
            ).one()
        self.assertEqual((count, distinct_count), (2, 1))
        self.assertIsNone(self._constraint_definition(FORMAT_CONSTRAINT))
        self.assertIsNone(self._constraint_definition(UNIQUE_CONSTRAINT))

    def test_application_collision_retry_uses_named_constraint(self):
        migrate(self.engine, schema=self.schema)
        owner = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status, "
                    "owner_customer_id, public_reference) VALUES "
                    "('Existing', 2, '2026-08-01', '18:00', "
                    "'pending', :owner, :reference)"
                ),
                {"owner": owner, "reference": REFERENCE_A},
            )

        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        insert_attempts = []
        observer = self._reservation_insert_observer(insert_attempts)
        event.listen(self.engine, "before_cursor_execute", observer)
        try:
            with patch(
                "app.db.repositories.reservation_repository."
                "generate_public_reference",
                side_effect=[REFERENCE_A, REFERENCE_B],
            ) as generator, Session() as db:
                created = ReservationService().create_reservation(
                    db,
                    ReservationCreate(
                        name="New",
                        people=4,
                        date="2026-08-01",
                        time="19:00",
                    ),
                    owner,
                )
        finally:
            event.remove(self.engine, "before_cursor_execute", observer)

        self.assertEqual(created.reference, REFERENCE_B)
        self.assertEqual(generator.call_count, 2)
        self.assertEqual(len(insert_attempts), 2)
        with Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(Reservation)
                ),
                2,
            )

    def test_application_collision_exhaustion_rolls_back_outer_unit(self):
        migrate(self.engine, schema=self.schema)
        owner = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status, "
                    "owner_customer_id, public_reference) VALUES "
                    "('Existing', 2, '2026-08-01', '18:00', "
                    "'pending', :owner, :reference)"
                ),
                {"owner": owner, "reference": REFERENCE_A},
            )

        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        insert_attempts = []
        observer = self._reservation_insert_observer(insert_attempts)
        event.listen(self.engine, "before_cursor_execute", observer)
        try:
            with patch(
                "app.db.repositories.reservation_repository."
                "generate_public_reference",
                return_value=REFERENCE_A,
            ) as generator, Session() as db:
                with self.assertRaises(
                    PublicReservationReferenceCollisionError
                ) as captured:
                    ReservationService().create_reservation(
                        db,
                        ReservationCreate(
                            name="Never persisted",
                            people=4,
                            date="2026-08-01",
                            time="19:00",
                        ),
                        owner,
                    )
                self.assertFalse(db.new)
                self.assertTrue(db.is_active)
        finally:
            event.remove(self.engine, "before_cursor_execute", observer)

        self.assertEqual(
            generator.call_count,
            PUBLIC_REFERENCE_MAX_ATTEMPTS,
        )
        self.assertEqual(
            len(insert_attempts),
            PUBLIC_REFERENCE_MAX_ATTEMPTS,
        )
        self.assertEqual(
            str(captured.exception),
            "RESERVATION_REFERENCE_UNAVAILABLE",
        )
        self.assertNotIn(REFERENCE_A, str(captured.exception))
        with Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(Reservation)
                ),
                1,
            )

    def test_application_unrelated_unique_violation_is_not_retried(self):
        migrate(self.engine, schema=self.schema)
        owner = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE reservations ADD CONSTRAINT "
                    "uq_reservations_name UNIQUE (name)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO reservations "
                    "(name, people, date, time, status, "
                    "owner_customer_id, public_reference) VALUES "
                    "('Existing', 2, '2026-08-01', '18:00', "
                    "'pending', :owner, :reference)"
                ),
                {"owner": owner, "reference": REFERENCE_A},
            )

        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        insert_attempts = []
        observer = self._reservation_insert_observer(insert_attempts)
        event.listen(self.engine, "before_cursor_execute", observer)
        try:
            with patch(
                "app.db.repositories.reservation_repository."
                "generate_public_reference",
                return_value=REFERENCE_B,
            ) as generator, Session() as db:
                with self.assertRaises(PersistenceOperationError) as captured:
                    ReservationService().create_reservation(
                        db,
                        ReservationCreate(
                            name="Existing",
                            people=4,
                            date="2026-08-01",
                            time="19:00",
                        ),
                        owner,
                    )
        finally:
            event.remove(self.engine, "before_cursor_execute", observer)

        cause = captured.exception.__cause__
        self.assertIsInstance(cause, IntegrityError)
        self.assertEqual(
            getattr(getattr(cause.orig, "diag", None), "constraint_name", None),
            "uq_reservations_name",
        )
        self.assertEqual(generator.call_count, 1)
        self.assertEqual(len(insert_attempts), 1)
        with Session() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(Reservation)
                ),
                1,
            )

    def test_downgrade_refuses_destructive_reference_removal(self):
        with self.assertRaises(
            PublicReservationReferenceMigrationError
        ):
            downgrade()


if __name__ == "__main__":
    unittest.main()
