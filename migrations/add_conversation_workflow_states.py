"""Convergently add restart-safe reservation workflow state."""

from pathlib import Path
import re
import sys

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Uuid,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


TABLE_NAME = "conversation_workflow_states"
PRIMARY_KEY_NAME = "pk_conversation_workflow_states"
OWNER_FOREIGN_KEY_NAME = (
    "fk_conversation_workflow_states_owner_customer_id_customers"
)
OWNER_SESSION_UNIQUE_NAME = (
    "uq_conversation_workflow_states_owner_session"
)
SCHEMA_VERSION_CHECK_NAME = (
    "ck_conversation_workflow_states_schema_version"
)
REVISION_CHECK_NAME = "ck_conversation_workflow_states_revision"
SESSION_HASH_CHECK_NAME = (
    "ck_conversation_workflow_states_session_hash_length"
)
PAYLOAD_OBJECT_CHECK_NAME = (
    "ck_conversation_workflow_states_payload_object"
)

INDEXES = {
    "ix_conversation_workflow_states_owner_customer_id": (
        "owner_customer_id",
    ),
    "ix_conversation_workflow_states_updated_at": ("updated_at",),
}

EXPECTED_COLUMNS = {
    "id": ("integer", False),
    "owner_customer_id": ("uuid", False),
    "session_reference_hash": ("varchar:64", False),
    "schema_version": ("integer", False),
    "payload": ("jsonb", False),
    "is_active": ("boolean", False),
    "revision": ("integer", False),
    "created_at": ("timestamptz", False),
    "updated_at": ("timestamptz", False),
}


def _quoted_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier supplied to migration.")
    return f'"{value}"'


def _qualified_table(schema: str | None, table: str) -> str:
    table_name = _quoted_identifier(table)
    return (
        f"{_quoted_identifier(schema)}.{table_name}"
        if schema
        else table_name
    )


def _type_is_compatible(actual_type, expected: str) -> bool:
    if expected == "integer":
        return isinstance(actual_type, Integer)
    if expected == "uuid":
        return (
            isinstance(actual_type, Uuid)
            or actual_type.__class__.__name__.upper() == "UUID"
        )
    if expected == "boolean":
        return isinstance(actual_type, Boolean)
    if expected == "jsonb":
        return (
            isinstance(actual_type, JSONB)
            or actual_type.__class__.__name__.upper() == "JSONB"
        ) and not (
            type(actual_type) is JSON
            or actual_type.__class__.__name__.upper() == "JSON"
        )
    if expected == "timestamptz":
        return isinstance(actual_type, DateTime) and bool(
            getattr(actual_type, "timezone", False)
        )
    if expected.startswith("varchar:"):
        length = int(expected.split(":", 1)[1])
        return (
            isinstance(actual_type, String)
            and getattr(actual_type, "length", None) == length
        )
    return False


def _validate_columns(inspector, schema: str | None) -> None:
    columns = {
        column["name"]: column
        for column in inspector.get_columns(TABLE_NAME, schema=schema)
    }
    missing = sorted(set(EXPECTED_COLUMNS) - set(columns))
    if missing:
        raise RuntimeError(
            "Existing conversation_workflow_states table is missing "
            "required columns: " + ", ".join(missing)
        )
    for name, (expected_type, expected_nullable) in EXPECTED_COLUMNS.items():
        column = columns[name]
        if not _type_is_compatible(column["type"], expected_type):
            raise RuntimeError(
                f"Existing {TABLE_NAME}.{name} has an incompatible type."
            )
        if bool(column.get("nullable")) != expected_nullable:
            raise RuntimeError(
                f"Existing {TABLE_NAME}.{name} has incompatible nullability."
            )

    id_column = columns["id"]
    id_default = str(id_column.get("default") or "").lower()
    if not id_column.get("identity") and "nextval" not in id_default:
        raise RuntimeError(
            f"Existing {TABLE_NAME}.id is not sequence or identity generated."
        )
    primary_key = inspector.get_pk_constraint(TABLE_NAME, schema=schema)
    if (
        primary_key.get("name") != PRIMARY_KEY_NAME
        or primary_key.get("constrained_columns") != ["id"]
    ):
        raise RuntimeError(
            "Existing conversation workflow primary key is incompatible."
        )


def _ensure_owner_foreign_key(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
) -> bool:
    expected_schemas = {schema, None}
    foreign_keys = inspector.get_foreign_keys(TABLE_NAME, schema=schema)
    for foreign_key in foreign_keys:
        if foreign_key.get("name") == OWNER_FOREIGN_KEY_NAME:
            if (
                foreign_key.get("constrained_columns")
                != ["owner_customer_id"]
                or foreign_key.get("referred_table") != "customers"
                or foreign_key.get("referred_columns") != ["id"]
                or foreign_key.get("referred_schema") not in expected_schemas
            ):
                raise RuntimeError(
                    "Existing workflow owner foreign key is incompatible."
                )
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
        f"ALTER TABLE {table_ref} "
        f"ADD CONSTRAINT {_quoted_identifier(OWNER_FOREIGN_KEY_NAME)} "
        f"FOREIGN KEY (owner_customer_id) REFERENCES {customers_ref} (id)"
    ))
    return True


def _ensure_owner_session_unique(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
) -> bool:
    candidates = [
        (
            item.get("name"),
            item.get("column_names"),
            True,
        )
        for item in inspector.get_unique_constraints(TABLE_NAME, schema=schema)
    ] + [
        (
            item.get("name"),
            item.get("column_names"),
            bool(item.get("unique")),
        )
        for item in inspector.get_indexes(TABLE_NAME, schema=schema)
    ]
    expected_columns = [
        "owner_customer_id",
        "session_reference_hash",
    ]
    for name, columns, is_unique in candidates:
        if name == OWNER_SESSION_UNIQUE_NAME:
            if columns != expected_columns or not is_unique:
                raise RuntimeError(
                    "Existing workflow owner/session uniqueness is incompatible."
                )
            return False
        if columns == expected_columns and is_unique:
            return False
    connection.execute(text(
        f"CREATE UNIQUE INDEX "
        f"{_quoted_identifier(OWNER_SESSION_UNIQUE_NAME)} "
        f"ON {table_ref} (owner_customer_id, session_reference_hash)"
    ))
    return True


def _check_definition_matches(name: str, sqltext) -> bool:
    normalized = " ".join(str(sqltext or "").lower().split())
    if name == SCHEMA_VERSION_CHECK_NAME:
        # Accept exact known v2 as a safe newer state without downgrading it
        # when this idempotent initial migration is rerun.
        return _workflow_schema_version(normalized) in {1, 2}
    if name == REVISION_CHECK_NAME:
        return "revision" in normalized and re.search(
            r"revision\s*>=\s*1", normalized
        ) is not None
    if name == SESSION_HASH_CHECK_NAME:
        return (
            "session_reference_hash" in normalized
            and "char_length" in normalized
            and re.search(r"=\s*64", normalized) is not None
        )
    if name == PAYLOAD_OBJECT_CHECK_NAME:
        return (
            "jsonb_typeof" in normalized
            and "payload" in normalized
            and "'object'" in normalized
        )
    return False


def _strip_outer_parentheses(value: str) -> str:
    candidate = value.strip()
    while candidate.startswith("(") and candidate.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(candidate):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    return candidate
                if depth == 0 and index != len(candidate) - 1:
                    encloses_all = False
                    break
        if depth != 0 or not encloses_all:
            break
        candidate = candidate[1:-1].strip()
    return candidate


_WORKFLOW_VERSION_COLUMN = (
    r'(?:(?:"schema_version")|schema_version)(?:::\s*integer)?'
)
_WORKFLOW_VERSION_V1 = re.compile(
    rf"{_WORKFLOW_VERSION_COLUMN}\s*=\s*1",
    re.IGNORECASE,
)
_WORKFLOW_VERSION_V2_IN = re.compile(
    rf"{_WORKFLOW_VERSION_COLUMN}\s+IN\s*\(\s*1\s*,\s*2\s*\)",
    re.IGNORECASE,
)
_WORKFLOW_VERSION_V2_ANY = re.compile(
    rf"{_WORKFLOW_VERSION_COLUMN}\s*=\s*ANY\s*\(\s*ARRAY\s*"
    rf"\[\s*1\s*,\s*2\s*\](?:::\s*integer\[\])?\s*\)",
    re.IGNORECASE,
)


def _workflow_schema_version(sqltext) -> int | None:
    candidate = _strip_outer_parentheses(str(sqltext or ""))
    if _WORKFLOW_VERSION_V1.fullmatch(candidate) is not None:
        return 1
    if (
        _WORKFLOW_VERSION_V2_IN.fullmatch(candidate) is not None
        or _WORKFLOW_VERSION_V2_ANY.fullmatch(candidate) is not None
    ):
        return 2
    return None


CHECKS = {
    SCHEMA_VERSION_CHECK_NAME: "schema_version = 1",
    REVISION_CHECK_NAME: "revision >= 1",
    SESSION_HASH_CHECK_NAME: "char_length(session_reference_hash) = 64",
    PAYLOAD_OBJECT_CHECK_NAME: "jsonb_typeof(payload) = 'object'",
}


def _ensure_checks(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
) -> bool:
    constraints = inspector.get_check_constraints(
        TABLE_NAME,
        schema=schema,
    )
    existing = {
        item.get("name"): item
        for item in constraints
    }
    schema_constraints = [
        item
        for item in constraints
        if "schema_version" in str(item.get("sqltext") or "").lower()
    ]
    owned_schema_constraint = existing.get(SCHEMA_VERSION_CHECK_NAME)
    if owned_schema_constraint is None:
        if schema_constraints:
            raise RuntimeError(
                "Existing workflow schema-version constraint is incompatible."
            )
    elif any(item is not owned_schema_constraint for item in schema_constraints):
        raise RuntimeError(
            "Existing workflow schema-version constraint is incompatible."
        )
    changed = False
    for name, expression in CHECKS.items():
        constraint = existing.get(name)
        if constraint is not None:
            if not _check_definition_matches(
                name,
                constraint.get("sqltext"),
            ):
                raise RuntimeError(
                    f"Existing constraint {name} is incompatible."
                )
            continue
        connection.execute(text(
            f"ALTER TABLE {table_ref} "
            f"ADD CONSTRAINT {_quoted_identifier(name)} "
            f"CHECK ({expression})"
        ))
        changed = True
    return changed


def _ensure_index(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
    name: str,
    columns: tuple[str, ...],
) -> bool:
    indexes = {
        item.get("name"): item
        for item in inspector.get_indexes(TABLE_NAME, schema=schema)
    }
    existing = indexes.get(name)
    if existing is not None:
        if (
            tuple(existing.get("column_names") or ()) != columns
            or bool(existing.get("unique"))
        ):
            raise RuntimeError(f"Existing index {name} is incompatible.")
        return False
    column_sql = ", ".join(
        _quoted_identifier(column)
        for column in columns
    )
    connection.execute(text(
        f"CREATE INDEX {_quoted_identifier(name)} "
        f"ON {table_ref} ({column_sql})"
    ))
    return True


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Create or converge workflow state without touching reservations."""

    target_engine = target_engine or engine
    table_ref = _qualified_table(schema, TABLE_NAME)
    customers_ref = _qualified_table(schema, "customers")
    changed = False

    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("customers", schema=schema):
            raise RuntimeError(
                "Tabel customers tidak ditemukan; migrasi dibatalkan."
            )
        if not inspector.has_table(TABLE_NAME, schema=schema):
            connection.execute(text(f"""
                CREATE TABLE {table_ref} (
                    id SERIAL NOT NULL,
                    owner_customer_id UUID NOT NULL,
                    session_reference_hash VARCHAR(64) NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    payload JSONB NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT {_quoted_identifier(PRIMARY_KEY_NAME)}
                        PRIMARY KEY (id),
                    CONSTRAINT {_quoted_identifier(OWNER_FOREIGN_KEY_NAME)}
                        FOREIGN KEY (owner_customer_id)
                        REFERENCES {customers_ref} (id),
                    CONSTRAINT {_quoted_identifier(OWNER_SESSION_UNIQUE_NAME)}
                        UNIQUE (owner_customer_id, session_reference_hash),
                    CONSTRAINT {_quoted_identifier(SCHEMA_VERSION_CHECK_NAME)}
                        CHECK ({CHECKS[SCHEMA_VERSION_CHECK_NAME]}),
                    CONSTRAINT {_quoted_identifier(REVISION_CHECK_NAME)}
                        CHECK ({CHECKS[REVISION_CHECK_NAME]}),
                    CONSTRAINT {_quoted_identifier(SESSION_HASH_CHECK_NAME)}
                        CHECK ({CHECKS[SESSION_HASH_CHECK_NAME]}),
                    CONSTRAINT {_quoted_identifier(PAYLOAD_OBJECT_CHECK_NAME)}
                        CHECK ({CHECKS[PAYLOAD_OBJECT_CHECK_NAME]})
                )
            """))
            changed = True
        else:
            _validate_columns(inspector, schema)
            changed |= _ensure_owner_foreign_key(
                connection,
                inspector,
                table_ref,
                schema,
            )
            inspector = inspect(connection)
            changed |= _ensure_owner_session_unique(
                connection,
                inspector,
                table_ref,
                schema,
            )
            inspector = inspect(connection)
            changed |= _ensure_checks(
                connection,
                inspector,
                table_ref,
                schema,
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
    return bool(changed)


if __name__ == "__main__":
    migrate()
