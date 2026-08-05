"""Add fail-closed safe-content provenance to demo assistant completions."""

from pathlib import Path
import re
import sys

from sqlalchemy import String, inspect, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CONTENT_SAFETY_COLUMN = "content_safety_version"
CONTENT_SAFETY_LENGTH = 32
CONTENT_SAFETY_VERSION = "aura_demo_safe_v1"
CONTENT_SAFETY_CONSTRAINT = "ck_demo_chat_messages_content_safety_version"
CONTENT_SAFETY_EXPRESSION = (
    "content_safety_version IS NULL OR "
    "(role = 'assistant' AND "
    "content_safety_version = 'aura_demo_safe_v1')"
)


class DemoChatContentSafetyMigrationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Demo chat content-safety migration failed safely.")


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise DemoChatContentSafetyMigrationError()
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


def _constraint_is_compatible(value) -> bool:
    normalized = _normalized_check(value)
    expected = _normalized_check(CONTENT_SAFETY_EXPRESSION)
    return normalized == expected


def _catalog_column_metadata(inspector, *, schema: str | None) -> dict:
    bind = getattr(inspector, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        raise DemoChatContentSafetyMigrationError()
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
        raise DemoChatContentSafetyMigrationError()
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


def _validate_column(inspector, columns: dict, *, schema: str | None) -> None:
    related = {name for name in columns if name.startswith("content_safety_")}
    if related != {CONTENT_SAFETY_COLUMN}:
        raise DemoChatContentSafetyMigrationError()
    column = columns[CONTENT_SAFETY_COLUMN]
    catalog = _catalog_column_metadata(inspector, schema=schema)
    catalog_related = {
        name for name in catalog if name.startswith("content_safety_")
    }
    metadata = catalog.get(CONTENT_SAFETY_COLUMN)
    if (
        catalog_related != {CONTENT_SAFETY_COLUMN}
        or metadata is None
        or not isinstance(column.get("type"), String)
        or getattr(column["type"], "length", None) != CONTENT_SAFETY_LENGTH
        or bool(column.get("nullable")) is not True
        or column.get("default") is not None
        or column.get("identity") is not None
        or column.get("computed") is not None
        or bool(metadata.get("atthasdef"))
        or (metadata.get("attidentity") or "") != ""
        or (metadata.get("attgenerated") or "") != ""
    ):
        raise DemoChatContentSafetyMigrationError()


def _validate_constraint(
    inspector,
    checks: dict,
    *,
    schema: str | None,
    require: bool,
) -> None:
    competing = {
        name
        for name, item in checks.items()
        if CONTENT_SAFETY_COLUMN in _normalized_check(item.get("sqltext"))
        and name != CONTENT_SAFETY_CONSTRAINT
    }
    if competing:
        raise DemoChatContentSafetyMigrationError()
    item = checks.get(CONTENT_SAFETY_CONSTRAINT)
    if item is None:
        if require:
            raise DemoChatContentSafetyMigrationError()
        return
    validation = _catalog_check_validation(inspector, schema=schema)
    if (
        not _constraint_is_compatible(item.get("sqltext"))
        or validation.get(CONTENT_SAFETY_CONSTRAINT) is not True
    ):
        raise DemoChatContentSafetyMigrationError()


def validate_existing_phase_d_schema(
    inspector,
    *,
    schema: str | None,
    allow_absent: bool,
) -> bool:
    columns = {
        item["name"]: item
        for item in inspector.get_columns("demo_chat_messages", schema=schema)
    }
    checks = {
        item.get("name"): item
        for item in inspector.get_check_constraints(
            "demo_chat_messages",
            schema=schema,
        )
    }
    present = CONTENT_SAFETY_COLUMN in columns
    _validate_constraint(
        inspector,
        checks,
        schema=schema,
        require=False,
    )
    if not present:
        if CONTENT_SAFETY_CONSTRAINT in checks or not allow_absent:
            raise DemoChatContentSafetyMigrationError()
        return False
    _validate_column(inspector, columns, schema=schema)
    _validate_constraint(
        inspector,
        checks,
        schema=schema,
        require=True,
    )
    return True


def _migrate(target_engine, *, schema: str | None) -> bool:
    changed = False
    with target_engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise DemoChatContentSafetyMigrationError()
        inspector = inspect(connection)
        if not inspector.has_table("demo_chat_messages", schema=schema):
            raise DemoChatContentSafetyMigrationError()
        columns = {
            item["name"]: item
            for item in inspector.get_columns(
                "demo_chat_messages",
                schema=schema,
            )
        }
        if CONTENT_SAFETY_COLUMN not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {_table(schema)} "
                    f"ADD COLUMN {_quote(CONTENT_SAFETY_COLUMN)} "
                    f"VARCHAR({CONTENT_SAFETY_LENGTH}) NULL"
                )
            )
            changed = True
        else:
            _validate_column(inspector, columns, schema=schema)

        inspector = inspect(connection)
        checks = {
            item.get("name"): item
            for item in inspector.get_check_constraints(
                "demo_chat_messages",
                schema=schema,
            )
        }
        _validate_constraint(
            inspector,
            checks,
            schema=schema,
            require=False,
        )
        if CONTENT_SAFETY_CONSTRAINT not in checks:
            connection.execute(
                text(
                    f"ALTER TABLE {_table(schema)} "
                    f"ADD CONSTRAINT {_quote(CONTENT_SAFETY_CONSTRAINT)} "
                    f"CHECK ({CONTENT_SAFETY_EXPRESSION})"
                )
            )
            changed = True

        validate_existing_phase_d_schema(
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
    except DemoChatContentSafetyMigrationError:
        raise
    except SQLAlchemyError:
        raise DemoChatContentSafetyMigrationError() from None


if __name__ == "__main__":
    migrate()
