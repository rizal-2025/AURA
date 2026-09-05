"""Reference-only direct reservation API against guarded PostgreSQL."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.dependencies import get_current_customer
from app.api import reservation as reservation_api
from app.core.customer_identity import AuthenticatedCustomer
from app.db.database import get_db
from app.db.models.customer import Customer
from app.db.models.conversation_workflow_state import ConversationWorkflowState
from app.db.models.reservation import Reservation
from app.main import create_app
from tests.integration.disposable_schema import DisposableSchemaResources
from tests.test_persisted_reservation_update import PersistedUpdateContract


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return "TEST_DATABASE_URL is not configured."
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if parsed.database != "aura_test":
            return "TEST_DATABASE_URL must target the exact aura_test database."
    except Exception:
        return "TEST_DATABASE_URL is invalid."
    return None


SKIP_REASON = _skip_reason()
FROZEN_NOW = datetime(2026, 9, 5, 5, 53, tzinfo=timezone.utc)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class PublicReservationAPIPostgreSQLTests(PersistedUpdateContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(os.environ["TEST_DATABASE_URL"])
        with cls.admin.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        if identity != ("aura_test", "aura_test_runner"):
            cls.admin.dispose()
            raise RuntimeError("Dedicated PostgreSQL preflight identity did not match.")

        cls.schema = f"aura_public_reservation_api_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_public_reservation_api_test_",),
            dispose_admin=True,
        )
        cls.addClassCleanup(cls.resources.cleanup)
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        schema_url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={cls.schema},public"}
        )
        cls.engine = create_engine(schema_url, pool_pre_ping=True)
        cls.resources.track_engine(cls.engine)
        Customer.__table__.create(cls.engine)
        Reservation.__table__.create(cls.engine)
        ConversationWorkflowState.__table__.create(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)

    def setUp(self):
        self.clock_patch = patch.object(
            reservation_api.service,
            "clock",
            lambda: FROZEN_NOW,
        )
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        with self.Session() as db:
            db.execute(text(f'TRUNCATE TABLE "{self.schema}"."reservations" CASCADE'))
            db.execute(text(f'TRUNCATE TABLE "{self.schema}"."customers" CASCADE'))
            db.execute(
                text(
                    f'ALTER SEQUENCE "{self.schema}"."reservations_id_seq" '
                    "RESTART WITH 2147483001"
                )
            )
            self.owner = Customer()
            self.other_owner = Customer()
            db.add_all((self.owner, self.other_owner))
            db.commit()

        self.active_owner_id = self.owner.id
        self.app = create_app(
            SimpleNamespace(APP_ENV="test", APP_NAME="AURA", VERSION="test")
        )

        def database_dependency():
            with self.Session() as db:
                yield db

        def identity_dependency():
            return AuthenticatedCustomer(
                id=self.active_owner_id,
                token_version=1,
                is_active=True,
            )

        self.app.dependency_overrides[get_db] = database_dependency
        self.app.dependency_overrides[get_current_customer] = identity_dependency
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()

    @staticmethod
    def body(name="Rizal"):
        return {
            "name": name,
            "people": 2,
            "date": "2026-09-05",
            "time": "12:57",
        }

    def assert_reference_only(self, response, *, database_id):
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"reference", "name", "people", "date", "time", "status"})
        self.assertNotIn(str(database_id), response.text)
        self.assertNotIn("id", payload)
        self.assertNotIn("reservationId", payload)

    def test_full_owner_scoped_lifecycle_is_reference_only(self):
        created = self.client.post("/reservation/", json=self.body())
        self.assertEqual(created.status_code, 200)
        reference = created.json()["reference"]
        with self.Session() as db:
            row = db.scalar(select(Reservation).where(Reservation.public_reference == reference))
            database_id = row.id
        self.assert_reference_only(created, database_id=database_id)

        listed = self.client.get("/reservation/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(listed.json()["reservations"][0]["reference"], reference)
        self.assertNotIn(str(database_id), listed.text)

        mixed = reference[:4] + reference[4:].upper()
        detail = self.client.get(f"/reservation/{mixed}")
        self.assert_reference_only(detail, database_id=database_id)

        updated = self.client.patch(
            f"/reservation/{reference}",
            json={"people": 3},
        )
        self.assert_reference_only(updated, database_id=database_id)
        self.assertEqual(updated.json()["people"], 3)

        cancelled = self.client.post(f"/reservation/{reference}/cancel")
        self.assert_reference_only(cancelled, database_id=database_id)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        repeated = self.client.post(f"/reservation/{reference}/cancel")
        self.assertEqual(repeated.status_code, 404)

    def test_cross_owner_and_missing_have_the_same_public_result(self):
        created = self.client.post("/reservation/", json=self.body("Owner A"))
        reference = created.json()["reference"]
        self.active_owner_id = self.other_owner.id
        cross_owner = self.client.get(f"/reservation/{reference}")
        missing = self.client.get("/reservation/RSV_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
        self.assertEqual(cross_owner.status_code, 404)
        self.assertEqual(cross_owner.json(), missing.json())

    def test_past_date_update_is_rejected_without_changing_the_row(self):
        created = self.client.post(
            "/reservation/",
            json=self.body("Sherly"),
        )
        self.assertEqual(created.status_code, 200)
        reference = created.json()["reference"]

        rejected = self.client.patch(
            f"/reservation/{reference}",
            json={"date": "2025-07-12"},
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["code"], "PAST_RESERVATION_DATE")
        with self.Session() as db:
            row = db.scalar(
                select(Reservation).where(
                    Reservation.public_reference == reference
                )
            )
            self.assertEqual(
                (row.name, row.people, row.date, row.time),
                ("Sherly", 2, "2026-09-05", "12:57"),
            )

    def test_future_date_update_preserves_existing_time(self):
        created = self.client.post(
            "/reservation/",
            json=self.body("Sherly"),
        )
        self.assertEqual(created.status_code, 200)
        reference = created.json()["reference"]

        updated = self.client.patch(
            f"/reservation/{reference}",
            json={"date": "2026-09-06"},
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["date"], "2026-09-06")
        self.assertEqual(updated.json()["time"], "12:57")
        with self.Session() as db:
            row = db.scalar(
                select(Reservation).where(
                    Reservation.public_reference == reference
                )
            )
            self.assertEqual(row.date, "2026-09-06")
            self.assertEqual(row.time, "12:57")

    def test_same_day_past_time_update_is_rejected_without_changing_the_row(self):
        created = self.client.post(
            "/reservation/",
            json=self.body("Sherly"),
        )
        self.assertEqual(created.status_code, 200)
        reference = created.json()["reference"]

        rejected = self.client.patch(
            f"/reservation/{reference}",
            json={"time": "12:30"},
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["code"], "PAST_RESERVATION_TIME")
        with self.Session() as db:
            row = db.scalar(
                select(Reservation).where(
                    Reservation.public_reference == reference
                )
            )
            self.assertEqual(
                (row.name, row.people, row.date, row.time),
                ("Sherly", 2, "2026-09-05", "12:57"),
            )


if __name__ == "__main__":
    unittest.main()
