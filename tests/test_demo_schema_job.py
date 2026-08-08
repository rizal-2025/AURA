"""Safety tests for the controlled empty-database schema runner."""

import os
import unittest
from unittest.mock import patch

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, text
from sqlalchemy.pool import StaticPool

from app.core.config import clear_settings_cache
from app.core.config_validation import (
    CFG_DEMO_DATABASE_NAME_INVALID,
    ConfigurationError,
)
from app.jobs.demo_schema import (
    apply_empty_schema,
    inspect_schema_state,
    is_exact_restore_verification_url,
    render_state,
    resolve_schema_database_url,
)


RESTORE_URL = (
    "postgresql+psycopg://aura_migration_owner@127.0.0.1:5432/"
    "aura_restore_test"
)


class DemoSchemaJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        def register_postgresql_compatibility(connection, _record):
            connection.create_function("char_length", 1, len)
            connection.create_function("jsonb_typeof", 1, lambda _value: "object")

        event.listen(self.engine, "connect", register_postgresql_compatibility)

    def tearDown(self):
        self.engine.dispose()

    def test_empty_schema_is_classified_as_additive_only(self):
        state = inspect_schema_state(self.engine)
        payload = render_state(state, operation="plan")
        self.assertTrue(state.empty)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["classification"], "additive-empty-schema")

    def test_apply_empty_schema_converges_and_is_idempotent(self):
        first = apply_empty_schema(self.engine)
        second = apply_empty_schema(self.engine)
        self.assertTrue(first.exact)
        self.assertTrue(second.exact)
        self.assertEqual(first.actual_tables, first.expected_tables)

    def test_nonempty_unknown_schema_is_blocked_without_mutation(self):
        metadata = MetaData()
        Table("unrelated", metadata, Column("id", Integer, primary_key=True))
        metadata.create_all(self.engine)
        before = inspect_schema_state(self.engine)
        with self.assertRaises(RuntimeError):
            apply_empty_schema(self.engine)
        after = inspect_schema_state(self.engine)
        self.assertEqual(before, after)
        self.assertFalse(after.exact)

    def test_converged_names_with_missing_structure_are_blocked(self):
        apply_empty_schema(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DROP INDEX ix_conversation_workflow_states_owner_customer_id"
                )
            )
        state = inspect_schema_state(self.engine)
        self.assertFalse(state.exact)
        with self.assertRaises(RuntimeError):
            apply_empty_schema(self.engine)

    def test_schema_inspection_executes_only_read_statements(self):
        apply_empty_schema(self.engine)
        statements = []

        def capture_statement(_connection, _cursor, statement, *_args):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.engine, "before_cursor_execute", capture_statement)
        state = inspect_schema_state(self.engine)
        event.remove(self.engine, "before_cursor_execute", capture_statement)

        self.assertTrue(state.exact)
        self.assertTrue(statements)
        self.assertEqual(set(statements) - {"SELECT", "PRAGMA"}, set())


class RestoreVerificationTargetTests(unittest.TestCase):
    def tearDown(self):
        clear_settings_cache()

    def test_global_demo_policy_rejects_restore_database_name(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "demo",
                "AURA_DISABLE_DOTENV": "1",
                "DEMO_DATABASE_URL": RESTORE_URL,
            },
            clear=False,
        ):
            for operation in ("plan", "apply-empty-schema"):
                with self.subTest(operation=operation):
                    clear_settings_cache()
                    with self.assertRaises(ConfigurationError) as raised:
                        resolve_schema_database_url(operation=operation)
                    self.assertEqual(
                        str(raised.exception),
                        CFG_DEMO_DATABASE_NAME_INVALID,
                    )

    def test_verify_accepts_only_exact_password_free_restore_target(self):
        self.assertTrue(is_exact_restore_verification_url(RESTORE_URL))
        invalid = (
            f" {RESTORE_URL}",
            RESTORE_URL.replace("aura_migration_owner", "postgres"),
            RESTORE_URL.replace("127.0.0.1", "localhost"),
            RESTORE_URL.replace(":5432", ":5433"),
            RESTORE_URL.replace("aura_restore_test", "aura_demo_public"),
            RESTORE_URL.replace("@", ":secret@"),
            RESTORE_URL + "?sslmode=disable",
            RESTORE_URL.replace("postgresql+psycopg", "postgresql"),
        )
        for value in invalid:
            with self.subTest(value=value.rsplit("/", 1)[-1]):
                self.assertFalse(is_exact_restore_verification_url(value))

    def test_verify_bypasses_only_database_name_policy(self):
        with patch.dict(os.environ, {"DEMO_DATABASE_URL": RESTORE_URL}, clear=False):
            with patch("app.jobs.demo_schema.get_database_settings") as settings:
                selected = resolve_schema_database_url(operation="verify")
        self.assertEqual(selected, RESTORE_URL)
        settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
