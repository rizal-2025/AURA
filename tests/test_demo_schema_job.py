"""Safety tests for the controlled empty-database schema runner."""

import unittest

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, text
from sqlalchemy.pool import StaticPool

from app.jobs.demo_schema import (
    apply_empty_schema,
    inspect_schema_state,
    render_state,
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


if __name__ == "__main__":
    unittest.main()
