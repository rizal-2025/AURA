"""Controlled, metadata-only schema gate for a new demo database."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
)

from app.core.config import get_database_settings, get_environment_settings
from app.db.base import Base
import app.db.models.reservation  # noqa: F401
import app.db.models.customer  # noqa: F401
import app.db.models.support_ticket  # noqa: F401
import app.db.models.telegram_identity  # noqa: F401
import app.db.models.support_ticket_notification  # noqa: F401
import app.db.models.conversation_workflow_state  # noqa: F401
import app.db.models.demo_persistence  # noqa: F401


SAFE_FAILURE_CODE = "DEMO_SCHEMA_OPERATION_FAILED"


@dataclass(frozen=True)
class SchemaState:
    expected_tables: int
    actual_tables: int
    expected_columns: int
    matching_columns: int
    matching_primary_keys: int
    matching_table_structures: int
    exact: bool

    @property
    def empty(self) -> bool:
        return self.actual_tables == 0


def inspect_schema_state(engine) -> SchemaState:
    """Compare names and primary-key metadata without reading row data."""
    inspector = inspect(engine)
    schema = "public" if engine.dialect.name == "postgresql" else None
    actual_tables = set(inspector.get_table_names(schema=schema))
    expected = {table.name: table for table in Base.metadata.sorted_tables}
    matching_columns = 0
    expected_columns = 0
    matching_primary_keys = 0
    matching_table_structures = 0

    for table_name, table in expected.items():
        expected_column_names = {column.name for column in table.columns}
        expected_columns += len(expected_column_names)
        if table_name not in actual_tables:
            continue
        actual_column_names = {
            item["name"]
            for item in inspector.get_columns(table_name, schema=schema)
        }
        if actual_column_names == expected_column_names:
            matching_columns += len(expected_column_names)
        expected_primary_key = {column.name for column in table.primary_key.columns}
        actual_primary_key = set(
            inspector.get_pk_constraint(table_name, schema=schema).get(
                "constrained_columns"
            )
            or ()
        )
        if actual_primary_key == expected_primary_key:
            matching_primary_keys += 1

        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(
                table_name,
                schema=schema,
            )
        }
        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        actual_foreign_keys = {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
            )
            for item in inspector.get_foreign_keys(table_name, schema=schema)
        }
        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        actual_checks = (
            {
                item.get("name")
                for item in inspector.get_check_constraints(
                    table_name,
                    schema=schema,
                )
            }
            if engine.dialect.name == "postgresql"
            else expected_checks
        )
        expected_indexes = {
            (bool(index.unique), tuple(column.name for column in index.columns))
            for index in table.indexes
        }
        actual_indexes = {
            (
                bool(item.get("unique")),
                tuple(item.get("column_names") or ()),
            )
            for item in inspector.get_indexes(table_name, schema=schema)
            if item.get("duplicates_constraint") is None
        }
        if (
            actual_unique == expected_unique
            and actual_foreign_keys == expected_foreign_keys
            and actual_checks == expected_checks
            and actual_indexes == expected_indexes
        ):
            matching_table_structures += 1

    exact = (
        actual_tables == set(expected)
        and matching_columns == expected_columns
        and matching_primary_keys == len(expected)
        and matching_table_structures == len(expected)
    )
    return SchemaState(
        expected_tables=len(expected),
        actual_tables=len(actual_tables),
        expected_columns=expected_columns,
        matching_columns=matching_columns,
        matching_primary_keys=matching_primary_keys,
        matching_table_structures=matching_table_structures,
        exact=exact,
    )


def render_state(state: SchemaState, *, operation: str) -> dict[str, object]:
    if state.exact:
        status = "verified"
        classification = "converged"
    elif state.empty:
        status = "ready"
        classification = "additive-empty-schema"
    else:
        status = "blocked"
        classification = "nonempty-schema-review-required"
    return {
        "status": status,
        "operation": operation,
        "classification": classification,
        "expectedTableCount": state.expected_tables,
        "actualTableCount": state.actual_tables,
        "expectedColumnCount": state.expected_columns,
        "matchingColumnCount": state.matching_columns,
        "matchingPrimaryKeyCount": state.matching_primary_keys,
        "matchingTableStructureCount": state.matching_table_structures,
    }


def apply_empty_schema(engine) -> SchemaState:
    before = inspect_schema_state(engine)
    if before.exact:
        return before
    if not before.empty:
        raise RuntimeError("schema-review-required")
    Base.metadata.create_all(bind=engine)
    after = inspect_schema_state(engine)
    if not after.exact:
        raise RuntimeError("schema-verification-failed")
    return after


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, apply, or verify the isolated demo schema.",
    )
    parser.add_argument(
        "--operation",
        required=True,
        choices=("plan", "apply-empty-schema", "verify"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = None
    try:
        if get_environment_settings().APP_ENV != "demo":
            raise RuntimeError("demo-only")
        settings = get_database_settings()
        engine = create_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
            pool_pre_ping=True,
        )
        if args.operation == "apply-empty-schema":
            state = apply_empty_schema(engine)
        else:
            state = inspect_schema_state(engine)
            if args.operation == "verify" and not state.exact:
                raise RuntimeError("schema-verification-failed")
        payload = render_state(state, operation=args.operation)
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["status"] != "blocked" else 1
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "code": SAFE_FAILURE_CODE},
                sort_keys=True,
            )
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
