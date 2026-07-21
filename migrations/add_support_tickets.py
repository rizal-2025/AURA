"""Convergently add persistent support tickets without touching reservations."""

from pathlib import Path
import re
import sys

from sqlalchemy import DateTime, Integer, String, Text, Uuid, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


PRIORITY_CHECK_NAME = "ck_support_tickets_priority"
STATUS_CHECK_NAME = "ck_support_tickets_status"
OWNER_FOREIGN_KEY_NAME = "fk_support_tickets_owner_customer_id_customers"
TICKET_NUMBER_UNIQUE_NAME = "uq_support_tickets_ticket_number"
ACTIVE_UNIQUE_INDEX_NAME = "uq_support_tickets_active_owner_session"
OBSOLETE_OWNER_SESSION_CONSTRAINT = "uq_support_ticket_owner_session"

PRIORITY_CHECK_SQL = "priority IN ('low', 'medium', 'high', 'urgent')"
STATUS_CHECK_SQL = "status IN ('open', 'in_progress', 'resolved', 'closed')"
ACTIVE_INDEX_PREDICATE = "status IN ('open', 'in_progress')"

INDEXES = {
    "ix_support_tickets_ticket_number": ("ticket_number",),
    "ix_support_tickets_owner_customer_id": ("owner_customer_id",),
    "ix_support_tickets_status": ("status",),
    "ix_support_tickets_created_at": ("created_at",),
}

EXPECTED_COLUMNS = {
    "id": ("integer", False),
    "ticket_number": ("varchar:32", False),
    "owner_customer_id": ("uuid", False),
    "session_reference_hash": ("varchar:64", False),
    "category": ("varchar:64", False),
    "reason_code": ("varchar:64", False),
    "priority": ("varchar:16", False),
    "safe_summary": ("text", False),
    "status": ("varchar:16", False),
    "attempt_count": ("integer", False),
    "created_at": ("timestamptz", False),
    "updated_at": ("timestamptz", False),
    "resolved_at": ("timestamptz", True),
}


def _quoted_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier supplied to migration.")
    return f'"{value}"'


def _qualified_table(schema: str | None, table: str) -> str:
    table_name = _quoted_identifier(table)
    return f"{_quoted_identifier(schema)}.{table_name}" if schema else table_name


def _type_is_compatible(actual_type, expected: str) -> bool:
    if expected == "integer":
        return isinstance(actual_type, Integer)
    if expected == "uuid":
        return isinstance(actual_type, Uuid) or actual_type.__class__.__name__.upper() == "UUID"
    if expected == "text":
        return isinstance(actual_type, Text)
    if expected == "timestamptz":
        return isinstance(actual_type, DateTime) and bool(getattr(actual_type, "timezone", False))
    if expected.startswith("varchar:"):
        length = int(expected.split(":", 1)[1])
        return isinstance(actual_type, String) and getattr(actual_type, "length", None) == length
    return False


def _validate_columns(inspector, schema: str | None) -> None:
    columns = {
        column["name"]: column
        for column in inspector.get_columns("support_tickets", schema=schema)
    }
    missing = sorted(set(EXPECTED_COLUMNS) - set(columns))
    if missing:
        raise RuntimeError(
            "Existing support_tickets table is missing required columns: "
            + ", ".join(missing)
        )

    for name, (expected_type, expected_nullable) in EXPECTED_COLUMNS.items():
        column = columns[name]
        if not _type_is_compatible(column["type"], expected_type):
            raise RuntimeError(f"Existing support_tickets.{name} has an incompatible type.")
        if bool(column.get("nullable")) != expected_nullable:
            raise RuntimeError(f"Existing support_tickets.{name} has incompatible nullability.")

    id_column = columns["id"]
    id_default = str(id_column.get("default") or "").lower()
    if not id_column.get("identity") and "nextval" not in id_default:
        raise RuntimeError("Existing support_tickets.id is not sequence or identity generated.")

    primary_key = inspector.get_pk_constraint("support_tickets", schema=schema)
    if primary_key.get("constrained_columns") != ["id"]:
        raise RuntimeError("Existing support_tickets primary key must be the id column.")


def _constraint_definition_is_safe(sqltext, *, column: str, allowed_values: tuple[str, ...]) -> bool:
    normalized = " ".join(str(sqltext or "").lower().split())
    if column not in normalized or " or " in normalized or "true" in normalized:
        return False
    literal_values = set(re.findall(r"'([^']+)'", normalized))
    return literal_values == set(allowed_values)


def _ensure_check_constraint(connection, inspector, table_ref: str, schema: str | None, *, name: str, expression: str, column: str, allowed_values: tuple[str, ...]) -> bool:
    checks = {
        item.get("name"): item
        for item in inspector.get_check_constraints("support_tickets", schema=schema)
    }
    existing = checks.get(name)
    if existing is not None:
        if not _constraint_definition_is_safe(
            existing.get("sqltext"),
            column=column,
            allowed_values=allowed_values,
        ):
            raise RuntimeError(f"Existing constraint {name} does not match the required policy.")
        return False
    connection.execute(text(
        f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quoted_identifier(name)} CHECK ({expression})"
    ))
    return True


def _ensure_owner_foreign_key(connection, inspector, table_ref: str, schema: str | None) -> bool:
    foreign_keys = inspector.get_foreign_keys("support_tickets", schema=schema)
    expected_schemas = {schema, None}
    for foreign_key in foreign_keys:
        if foreign_key.get("name") == OWNER_FOREIGN_KEY_NAME:
            if (
                foreign_key.get("constrained_columns") != ["owner_customer_id"]
                or foreign_key.get("referred_table") != "customers"
                or foreign_key.get("referred_columns") != ["id"]
                or foreign_key.get("referred_schema") not in expected_schemas
            ):
                raise RuntimeError("Existing owner foreign key has an incompatible definition.")
            return False
        if (
            foreign_key.get("constrained_columns") == ["owner_customer_id"]
            and foreign_key.get("referred_table") == "customers"
            and foreign_key.get("referred_columns") == ["id"]
            and foreign_key.get("referred_schema") in expected_schemas
        ):
            return False

    customers_ref = _qualified_table(schema, "customers")
    connection.execute(text(
        f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quoted_identifier(OWNER_FOREIGN_KEY_NAME)} "
        f"FOREIGN KEY (owner_customer_id) REFERENCES {customers_ref} (id)"
    ))
    return True


def _ensure_ticket_number_uniqueness(connection, inspector, table_ref: str, schema: str | None) -> bool:
    unique_constraints = inspector.get_unique_constraints("support_tickets", schema=schema)
    indexes = inspector.get_indexes("support_tickets", schema=schema)
    candidates = [
        (item.get("name"), item.get("column_names"), True)
        for item in unique_constraints
    ] + [
        (item.get("name"), item.get("column_names"), bool(item.get("unique")))
        for item in indexes
    ]
    for name, columns, is_unique in candidates:
        if name == TICKET_NUMBER_UNIQUE_NAME and (columns != ["ticket_number"] or not is_unique):
            raise RuntimeError("Existing ticket-number uniqueness object is incompatible.")
        if columns == ["ticket_number"] and is_unique:
            return False

    connection.execute(text(
        f"CREATE UNIQUE INDEX {_quoted_identifier(TICKET_NUMBER_UNIQUE_NAME)} "
        f"ON {table_ref} (ticket_number)"
    ))
    return True


def _ensure_index(connection, inspector, table_ref: str, schema: str | None, name: str, columns: tuple[str, ...]) -> bool:
    indexes = {
        item.get("name"): item
        for item in inspector.get_indexes("support_tickets", schema=schema)
    }
    existing = indexes.get(name)
    if existing is not None:
        if tuple(existing.get("column_names") or ()) != columns:
            raise RuntimeError(f"Existing index {name} has incompatible columns.")
        return False
    column_sql = ", ".join(_quoted_identifier(column) for column in columns)
    connection.execute(text(
        f"CREATE INDEX {_quoted_identifier(name)} ON {table_ref} ({column_sql})"
    ))
    return True


def _ensure_active_unique_index(connection, inspector, table_ref: str, schema: str | None) -> bool:
    indexes = {
        item.get("name"): item
        for item in inspector.get_indexes("support_tickets", schema=schema)
    }
    existing = indexes.get(ACTIVE_UNIQUE_INDEX_NAME)
    if existing is not None:
        predicate_value = (existing.get("dialect_options") or {}).get("postgresql_where")
        predicate = "" if predicate_value is None else str(predicate_value).lower()
        predicate_values = set(re.findall(r"'([^']+)'", predicate))
        if (
            existing.get("column_names") != ["owner_customer_id", "session_reference_hash"]
            or not existing.get("unique")
            or "status" not in predicate
            or predicate_values != {"open", "in_progress"}
            or " or " in predicate
            or " and " in predicate
            or "true" in predicate
        ):
            raise RuntimeError("Existing active-ticket unique index is incompatible.")
        return False

    connection.execute(text(
        f"CREATE UNIQUE INDEX {_quoted_identifier(ACTIVE_UNIQUE_INDEX_NAME)} "
        f"ON {table_ref} (owner_customer_id, session_reference_hash) "
        f"WHERE {ACTIVE_INDEX_PREDICATE}"
    ))
    return True


def _remove_obsolete_owner_session_constraint(connection, inspector, table_ref: str, schema: str | None) -> bool:
    unique_constraints = inspector.get_unique_constraints("support_tickets", schema=schema)
    changed = False
    for constraint in unique_constraints:
        columns = constraint.get("column_names")
        name = constraint.get("name")
        if columns != ["owner_customer_id", "session_reference_hash"]:
            continue
        if name != OBSOLETE_OWNER_SESSION_CONSTRAINT:
            raise RuntimeError(
                "An unrecognized broad owner/session uniqueness constraint blocks the active-ticket lifecycle."
            )
        connection.execute(text(
            f"ALTER TABLE {table_ref} DROP CONSTRAINT {_quoted_identifier(name)}"
        ))
        changed = True
    return changed


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Create or converge support_tickets; never modify reservation data."""
    target_engine = target_engine or engine
    table_ref = _qualified_table(schema, "support_tickets")
    customers_ref = _qualified_table(schema, "customers")
    changed = False

    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("customers", schema=schema):
            raise RuntimeError("Tabel customers tidak ditemukan; migrasi dibatalkan.")

        if not inspector.has_table("support_tickets", schema=schema):
            connection.execute(text(f"""
                CREATE TABLE {table_ref} (
                    id SERIAL PRIMARY KEY,
                    ticket_number VARCHAR(32) NOT NULL,
                    owner_customer_id UUID NOT NULL,
                    session_reference_hash VARCHAR(64) NOT NULL,
                    category VARCHAR(64) NOT NULL,
                    reason_code VARCHAR(64) NOT NULL,
                    priority VARCHAR(16) NOT NULL,
                    safe_summary TEXT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'open',
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMPTZ NULL,
                    CONSTRAINT {_quoted_identifier(TICKET_NUMBER_UNIQUE_NAME)} UNIQUE (ticket_number),
                    CONSTRAINT {_quoted_identifier(OWNER_FOREIGN_KEY_NAME)}
                        FOREIGN KEY (owner_customer_id) REFERENCES {customers_ref} (id),
                    CONSTRAINT {_quoted_identifier(PRIORITY_CHECK_NAME)} CHECK ({PRIORITY_CHECK_SQL}),
                    CONSTRAINT {_quoted_identifier(STATUS_CHECK_NAME)} CHECK ({STATUS_CHECK_SQL})
                )
            """))
            changed = True
            inspector = inspect(connection)
        else:
            _validate_columns(inspector, schema)
            changed |= _ensure_owner_foreign_key(connection, inspector, table_ref, schema)
            changed |= _ensure_ticket_number_uniqueness(connection, inspector, table_ref, schema)
            changed |= _ensure_check_constraint(
                connection,
                inspector,
                table_ref,
                schema,
                name=PRIORITY_CHECK_NAME,
                expression=PRIORITY_CHECK_SQL,
                column="priority",
                allowed_values=("low", "medium", "high", "urgent"),
            )
            changed |= _ensure_check_constraint(
                connection,
                inspector,
                table_ref,
                schema,
                name=STATUS_CHECK_NAME,
                expression=STATUS_CHECK_SQL,
                column="status",
                allowed_values=("open", "in_progress", "resolved", "closed"),
            )

        inspector = inspect(connection)
        for index_name, columns in INDEXES.items():
            changed |= _ensure_index(
                connection,
                inspector,
                table_ref,
                schema,
                index_name,
                columns,
            )
            inspector = inspect(connection)

        changed |= _ensure_active_unique_index(connection, inspector, table_ref, schema)
        inspector = inspect(connection)
        changed |= _remove_obsolete_owner_session_constraint(
            connection,
            inspector,
            table_ref,
            schema,
        )
        return bool(changed)


if __name__ == "__main__":
    migrate()
