"""Add durable public-reference mutation metadata to demo chat completion."""

from pathlib import Path
import re
import sys

from sqlalchemy import String, inspect, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


MUTATION_COLUMNS = {
    "reservation_mutation_operation": (16, True),
    "reservation_mutation_reference": (36, True),
}
MUTATION_CONSTRAINTS = {
    "ck_demo_chat_messages_reservation_mutation_operation": (
        "reservation_mutation_operation IS NULL OR "
        "reservation_mutation_operation IN "
        "('created', 'updated', 'cancelled')"
    ),
    "ck_demo_chat_messages_reservation_mutation_pair": (
        "(reservation_mutation_operation IS NULL AND "
        "reservation_mutation_reference IS NULL) OR "
        "(reservation_mutation_operation IS NOT NULL AND "
        "reservation_mutation_reference IS NOT NULL)"
    ),
    "ck_demo_chat_messages_reservation_mutation_assistant_role": (
        "reservation_mutation_operation IS NULL OR role = 'assistant'"
    ),
    "ck_demo_chat_messages_reservation_mutation_reference": (
        "reservation_mutation_reference IS NULL OR "
        "reservation_mutation_reference ~ '^RSV_[0-9a-f]{32}$'"
    ),
}


class DemoChatReservationMutationMigrationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Demo chat reservation mutation migration failed safely.")


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise DemoChatReservationMutationMigrationError()
    return f'"{identifier}"'


def _table(schema: str | None) -> str:
    table = _quote("demo_chat_messages")
    return f"{_quote(schema)}.{table}" if schema else table


def _normalized_check(value) -> str:
    normalized = (
        str(value or "")
        .casefold()
        .replace("::text", "")
        .replace("::character varying", "")
        .replace('"', "")
    )
    return re.sub(r"[\s()]+", "", normalized)


def _constraint_is_compatible(name: str, value) -> bool:
    normalized = _normalized_check(value)
    if not normalized or "true" in normalized or "false" in normalized:
        return False
    literals = re.findall(r"'([^']*)'", normalized)
    operation = "reservation_mutation_operation"
    reference = "reservation_mutation_reference"

    if name.endswith("_operation"):
        return (
            set(literals) == {"created", "updated", "cancelled"}
            and len(literals) == 3
            and f"{operation}isnullor{operation}" in normalized
            and ("in'created','updated','cancelled'" in normalized
                 or "=anyarray['created','updated','cancelled']" in normalized)
        )
    if name.endswith("_pair"):
        expected = (
            f"{operation}isnulland{reference}isnullor"
            f"{operation}isnotnulland{reference}isnotnull"
        )
        return not literals and normalized == expected
    if name.endswith("_assistant_role"):
        return (
            literals == ["assistant"]
            and normalized
            == f"{operation}isnullorrole='assistant'"
        )
    if name.endswith("_reference"):
        return (
            literals == [r"^rsv_[0-9a-f]{32}$"]
            and normalized
            == f"{reference}isnullor{reference}~'^rsv_[0-9a-f]{{32}}$'"
        )
    return False


def _reject_competing_mutation_constraints(checks: dict) -> None:
    mutation_columns = tuple(MUTATION_COLUMNS)
    for name, item in checks.items():
        normalized = _normalized_check(item.get("sqltext"))
        if (
            any(column in normalized for column in mutation_columns)
            and name not in MUTATION_CONSTRAINTS
        ):
            raise DemoChatReservationMutationMigrationError()


def _column_definition_is_compatible(column, *, length: int, nullable: bool) -> bool:
    return (
        isinstance(column.get("type"), String)
        and getattr(column["type"], "length", None) == length
        and bool(column.get("nullable")) == nullable
        and column.get("default") is None
        and column.get("identity") is None
        and column.get("computed") is None
    )


def _catalog_column_metadata(inspector, *, schema: str | None) -> dict:
    bind = getattr(inspector, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        raise DemoChatReservationMutationMigrationError()
    rows = bind.execute(
        text(
            """
            SELECT
                attribute.attname,
                attribute.atthasdef,
                attribute.attidentity,
                attribute.attgenerated
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE relation.relname = :table_name
              AND namespace.nspname = COALESCE(:schema_name, current_schema())
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            """
        ),
        {
            "table_name": "demo_chat_messages",
            "schema_name": schema,
        },
    ).mappings()
    return {row["attname"]: row for row in rows}


def _catalog_check_validation(inspector, *, schema: str | None) -> dict:
    bind = getattr(inspector, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        raise DemoChatReservationMutationMigrationError()
    rows = bind.execute(
        text(
            """
            SELECT constraint_row.conname, constraint_row.convalidated
            FROM pg_catalog.pg_constraint AS constraint_row
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_row.conrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE constraint_row.contype = 'c'
              AND relation.relname = :table_name
              AND namespace.nspname = COALESCE(:schema_name, current_schema())
            """
        ),
        {
            "table_name": "demo_chat_messages",
            "schema_name": schema,
        },
    ).mappings()
    return {row["conname"]: bool(row["convalidated"]) for row in rows}


def _validate_column_metadata(inspector, columns: dict, *, schema: str | None) -> None:
    related_columns = {
        name
        for name in columns
        if name.startswith("reservation_mutation_")
    }
    if related_columns != set(MUTATION_COLUMNS):
        raise DemoChatReservationMutationMigrationError()

    catalog_columns = _catalog_column_metadata(inspector, schema=schema)
    catalog_related = {
        name
        for name in catalog_columns
        if name.startswith("reservation_mutation_")
    }
    if catalog_related != set(MUTATION_COLUMNS):
        raise DemoChatReservationMutationMigrationError()

    for column_name, (length, nullable) in MUTATION_COLUMNS.items():
        column = columns[column_name]
        catalog = catalog_columns.get(column_name)
        if (
            catalog is None
            or not _column_definition_is_compatible(
                column,
                length=length,
                nullable=nullable,
            )
            or bool(catalog.get("atthasdef"))
            or (catalog.get("attidentity") or "") != ""
            or (catalog.get("attgenerated") or "") != ""
        ):
            raise DemoChatReservationMutationMigrationError()


def _validate_known_constraints(
    inspector,
    checks: dict,
    *,
    schema: str | None,
    require_all: bool,
) -> None:
    _reject_competing_mutation_constraints(checks)
    known_checks = set(MUTATION_CONSTRAINTS).intersection(checks)
    if require_all and known_checks != set(MUTATION_CONSTRAINTS):
        raise DemoChatReservationMutationMigrationError()

    validation_state = _catalog_check_validation(inspector, schema=schema)
    for name in known_checks:
        if (
            not _constraint_is_compatible(name, checks[name].get("sqltext"))
            or validation_state.get(name) is not True
        ):
            raise DemoChatReservationMutationMigrationError()


def validate_existing_phase_c_schema(
    inspector,
    *,
    schema: str | None,
    allow_absent: bool,
) -> bool:
    columns = {
        item["name"]: item
        for item in inspector.get_columns("demo_chat_messages", schema=schema)
    }
    present = set(MUTATION_COLUMNS).intersection(columns)
    checks = {
        item.get("name"): item
        for item in inspector.get_check_constraints(
            "demo_chat_messages",
            schema=schema,
        )
    }
    _validate_known_constraints(
        inspector,
        checks,
        schema=schema,
        require_all=False,
    )
    known_checks = set(MUTATION_CONSTRAINTS).intersection(checks)

    if not present:
        if known_checks or not allow_absent:
            raise DemoChatReservationMutationMigrationError()
        return False
    if present != set(MUTATION_COLUMNS):
        raise DemoChatReservationMutationMigrationError()
    _validate_column_metadata(inspector, columns, schema=schema)
    _validate_known_constraints(
        inspector,
        checks,
        schema=schema,
        require_all=True,
    )
    return True


def _migrate(target_engine, *, schema: str | None) -> bool:
    changed = False
    with target_engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise DemoChatReservationMutationMigrationError()
        inspector = inspect(connection)
        if not inspector.has_table("demo_chat_messages", schema=schema):
            raise DemoChatReservationMutationMigrationError()

        columns = {
            item["name"]: item
            for item in inspector.get_columns(
                "demo_chat_messages",
                schema=schema,
            )
        }
        present = set(MUTATION_COLUMNS).intersection(columns)
        if present and present != set(MUTATION_COLUMNS):
            raise DemoChatReservationMutationMigrationError()
        if not present:
            connection.execute(
                text(
                    f"ALTER TABLE {_table(schema)} "
                    f"ADD COLUMN {_quote('reservation_mutation_operation')} "
                    "VARCHAR(16) NULL, "
                    f"ADD COLUMN {_quote('reservation_mutation_reference')} "
                    "VARCHAR(36) NULL"
                )
            )
            changed = True
        else:
            _validate_column_metadata(inspector, columns, schema=schema)

        inspector = inspect(connection)
        existing_checks = {
            item.get("name"): item
            for item in inspector.get_check_constraints(
                "demo_chat_messages",
                schema=schema,
            )
        }
        _validate_known_constraints(
            inspector,
            existing_checks,
            schema=schema,
            require_all=False,
        )

        for name, expression in MUTATION_CONSTRAINTS.items():
            inspector = inspect(connection)
            existing = next(
                (
                    item
                    for item in inspector.get_check_constraints(
                        "demo_chat_messages",
                        schema=schema,
                    )
                    if item.get("name") == name
                ),
                None,
            )
            if existing is not None:
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {_table(schema)} "
                    f"ADD CONSTRAINT {_quote(name)} CHECK ({expression})"
                )
            )
            changed = True

        validate_existing_phase_c_schema(
            inspect(connection),
            schema=schema,
            allow_absent=False,
        )
    return changed


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    if target_engine is None:
        from app.db.database import engine as default_engine

        target_engine = default_engine
    try:
        return _migrate(target_engine, schema=schema)
    except DemoChatReservationMutationMigrationError:
        raise
    except SQLAlchemyError:
        raise DemoChatReservationMutationMigrationError() from None


if __name__ == "__main__":
    migrate()
