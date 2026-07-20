import importlib.util
from datetime import timedelta
from pathlib import Path
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock, patch

import jwt
from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials

from app.api.auth import create_guest_customer
from app.api.dependencies import get_current_customer
from app.core.config import settings
from app.core.security import (
    JWT_ALGORITHM,
    create_customer_access_token,
    validate_customer_access_token,
)
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "add_secure_customer_identity.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "secure_customer_identity_migration",
    MIGRATION_PATH,
)
assert MIGRATION_SPEC is not None and MIGRATION_SPEC.loader is not None
migration = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(migration)


class FakeConnection:
    def __init__(self, state):
        self.state = state
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "CREATE TABLE IF NOT EXISTS customers" in sql:
            self.state["customers"] = True
        elif "ADD COLUMN IF NOT EXISTS owner_customer_id" in sql:
            self.state["owner_column"] = True
        elif "ADD CONSTRAINT fk_reservations_owner_customer_id" in sql:
            self.state["owner_foreign_key"] = True
        elif "CREATE INDEX IF NOT EXISTS ix_reservations_owner_customer_id_id" in sql:
            self.state["owner_index"] = True


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection


class FakeInspector:
    def __init__(self, state):
        self.state = state

    def has_table(self, table_name):
        if table_name == "reservations":
            return True
        if table_name == "customers":
            return self.state["customers"]
        return False

    def get_columns(self, table_name):
        columns = [{"name": "id"}]
        if self.state["owner_column"]:
            columns.append({"name": "owner_customer_id"})
        return columns

    def get_foreign_keys(self, table_name):
        if not self.state["owner_foreign_key"]:
            return []
        return [
            {
                "constrained_columns": ["owner_customer_id"],
                "referred_table": "customers",
            }
        ]

    def get_indexes(self, table_name):
        if not self.state["owner_index"]:
            return []
        return [{"name": "ix_reservations_owner_customer_id_id"}]


class TestSecureCustomerIdentity(unittest.TestCase):
    def setUp(self):
        self.original_secret = settings.AUTH_JWT_SECRET
        self.original_issuer = settings.AUTH_JWT_ISSUER
        self.original_audience = settings.AUTH_JWT_AUDIENCE
        settings.AUTH_JWT_SECRET = "test-secure-customer-secret"
        settings.AUTH_JWT_ISSUER = "aura-test"
        settings.AUTH_JWT_AUDIENCE = "aura-test-api"
        self.customer_id = uuid4()

    def tearDown(self):
        settings.AUTH_JWT_SECRET = self.original_secret
        settings.AUTH_JWT_ISSUER = self.original_issuer
        settings.AUTH_JWT_AUDIENCE = self.original_audience

    def _credentials(self, token):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def _customer(self, **overrides):
        values = {
            "id": self.customer_id,
            "is_active": True,
            "token_version": 1,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_customer_model_has_secure_owner_fields(self):
        self.assertTrue(Customer.__table__.c.id.primary_key)
        self.assertEqual(Customer.__table__.c.token_version.default.arg, 1)
        self.assertEqual(Customer.__table__.c.is_active.default.arg, True)
        self.assertIn("owner_customer_id", Reservation.__table__.c)
        self.assertTrue(Reservation.__table__.c.owner_customer_id.nullable)
        self.assertIn("customer_id", Reservation.__table__.c)

    def test_guest_customer_and_token_creation(self):
        created_customer = SimpleNamespace(id=self.customer_id, token_version=1)
        db = MagicMock()

        with (
            patch("app.api.auth.uuid4", return_value=self.customer_id) as generate_uuid,
            patch("app.api.auth.Customer", return_value=created_customer) as customer_model,
        ):
            http_response = Response()
            response = create_guest_customer(http_response, db)

        generate_uuid.assert_called_once_with()
        customer_model.assert_called_once_with(
            id=self.customer_id,
            token_version=1,
        )
        db.add.assert_called_once_with(created_customer)
        db.commit.assert_called_once()
        db.refresh.assert_called_once_with(created_customer)
        token_customer_id, token_version = validate_customer_access_token(
            response.access_token
        )
        self.assertEqual(token_customer_id, self.customer_id)
        self.assertEqual(token_version, 1)
        self.assertEqual(response.token_type, "bearer")
        self.assertEqual(http_response.headers["Cache-Control"], "no-store")

    def test_guest_customer_is_not_persisted_when_secret_is_missing(self):
        self.settings_secret(None)
        db = MagicMock()

        with self.assertRaises(HTTPException) as raised:
            create_guest_customer(Response(), db)

        self.assertEqual(raised.exception.status_code, 503)
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_valid_token_returns_active_customer(self):
        token, _ = create_customer_access_token(self.customer_id, 1)
        customer = self._customer()
        db = MagicMock()
        db.get.return_value = customer

        result = get_current_customer(self._credentials(token), db)

        self.assertIs(result, customer)
        db.get.assert_called_once_with(Customer, self.customer_id)

    def test_missing_token_is_rejected(self):
        db = MagicMock()

        with self.assertRaises(HTTPException) as raised:
            get_current_customer(None, db)

        self.assertEqual(raised.exception.status_code, 401)
        db.get.assert_not_called()

    def test_forged_token_is_rejected(self):
        payload = {
            "sub": str(self.customer_id),
            "token_version": 1,
            "iat": 1_700_000_000,
            "exp": 4_000_000_000,
            "iss": settings.AUTH_JWT_ISSUER,
            "aud": settings.AUTH_JWT_AUDIENCE,
        }
        token = jwt.encode(payload, "wrong-secret", algorithm=JWT_ALGORITHM)

        self._assert_unauthorized(token)

    def test_expired_token_is_rejected(self):
        token, _ = create_customer_access_token(
            self.customer_id,
            1,
            expires_delta=timedelta(seconds=-1),
        )

        self._assert_unauthorized(token)

    def test_wrong_issuer_is_rejected(self):
        token = self._custom_token(issuer="not-aura")

        self._assert_unauthorized(token)

    def test_wrong_audience_is_rejected(self):
        token = self._custom_token(audience="not-aura-api")

        self._assert_unauthorized(token)

    def test_inactive_customer_is_rejected(self):
        token, _ = create_customer_access_token(self.customer_id, 1)
        db = MagicMock()
        db.get.return_value = self._customer(is_active=False)

        with self.assertRaises(HTTPException) as raised:
            get_current_customer(self._credentials(token), db)

        self.assertEqual(raised.exception.status_code, 401)

    def test_token_version_mismatch_is_rejected(self):
        token, _ = create_customer_access_token(self.customer_id, 1)
        db = MagicMock()
        db.get.return_value = self._customer(token_version=2)

        with self.assertRaises(HTTPException) as raised:
            get_current_customer(self._credentials(token), db)

        self.assertEqual(raised.exception.status_code, 401)

    def test_migration_is_idempotent_and_preserves_existing_reservations(self):
        state = {
            "customers": False,
            "owner_column": False,
            "owner_foreign_key": False,
            "owner_index": False,
        }
        existing_reservations = [
            {"id": 1, "customer_id": "legacy-session", "name": "Rizal"}
        ]
        connection = FakeConnection(state)
        inspector = FakeInspector(state)

        with (
            patch.object(migration, "engine", FakeEngine(connection)),
            patch.object(migration, "inspect", return_value=inspector),
        ):
            self.assertTrue(migration.migrate())
            first_statement_count = len(connection.statements)
            self.assertFalse(migration.migrate())

        self.assertEqual(len(connection.statements), first_statement_count)
        self.assertEqual(existing_reservations[0]["customer_id"], "legacy-session")
        self.assertTrue(state["customers"])
        self.assertTrue(state["owner_column"])
        self.assertTrue(state["owner_foreign_key"])
        self.assertTrue(state["owner_index"])
        statements = " ".join(connection.statements).upper()
        self.assertNotIn("DELETE", statements)
        self.assertNotIn("UPDATE", statements)
        self.assertNotIn("DROP", statements)
        self.assertNotIn("TRUNCATE", statements)

    def _custom_token(self, *, issuer=None, audience=None):
        token, _ = create_customer_access_token(self.customer_id, 1)
        payload = jwt.decode(
            token,
            settings.AUTH_JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_signature": False},
        )
        if issuer is not None:
            payload["iss"] = issuer
        if audience is not None:
            payload["aud"] = audience
        return jwt.encode(payload, settings.AUTH_JWT_SECRET, algorithm=JWT_ALGORITHM)

    def _assert_unauthorized(self, token):
        db = MagicMock()
        with self.assertRaises(HTTPException) as raised:
            get_current_customer(self._credentials(token), db)
        self.assertEqual(raised.exception.status_code, 401)
        db.get.assert_not_called()

    def settings_secret(self, secret):
        settings.AUTH_JWT_SECRET = secret


if __name__ == "__main__":
    unittest.main()
