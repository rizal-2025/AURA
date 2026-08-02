"""Allow exact workflow snapshot schema versions one and two."""

from __future__ import annotations

from pathlib import Path
import re
import sys

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


TABLE = "conversation_workflow_states"
CONSTRAINT = "ck_conversation_workflow_states_schema_version"
V1_EXPRESSION = "schema_version = 1"
V2_EXPRESSION = "schema_version IN (1, 2)"


class WorkflowSchemaV2MigrationError(RuntimeError):
    code = "WORKFLOW_SCHEMA_V2_MIGRATION_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


def _quote(identifier: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier) is None:
        raise WorkflowSchemaV2MigrationError()
    return f'"{identifier}"'


def _table(schema: str | None) -> str:
    table = _quote(TABLE)
    return f"{_quote(schema)}.{table}" if schema else table


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


_COLUMN = r'(?:(?:"schema_version")|schema_version)(?:::\s*integer)?'
_V1_PATTERN = re.compile(rf"{_COLUMN}\s*=\s*1", re.IGNORECASE)
_V2_IN_PATTERN = re.compile(
    rf"{_COLUMN}\s+IN\s*\(\s*1\s*,\s*2\s*\)",
    re.IGNORECASE,
)
_V2_ANY_PATTERN = re.compile(
    rf"{_COLUMN}\s*=\s*ANY\s*\(\s*ARRAY\s*"
    rf"\[\s*1\s*,\s*2\s*\](?:::\s*integer\[\])?\s*\)",
    re.IGNORECASE,
)


def _constraint_version(sqltext) -> int | None:
    if not isinstance(sqltext, str):
        return None
    candidate = _strip_outer_parentheses(sqltext)
    if _V1_PATTERN.fullmatch(candidate) is not None:
        return 1
    if (
        _V2_IN_PATTERN.fullmatch(candidate) is not None
        or _V2_ANY_PATTERN.fullmatch(candidate) is not None
    ):
        return 2
    return None


def _schema_version_constraint(inspector, schema: str | None):
    constraints = inspector.get_check_constraints(TABLE, schema=schema)
    named = [item for item in constraints if item.get("name") == CONSTRAINT]
    if len(named) > 1:
        raise WorkflowSchemaV2MigrationError()
    related = [
        item
        for item in constraints
        if "schema_version" in str(item.get("sqltext") or "").lower()
    ]
    if named:
        if any(item is not named[0] for item in related):
            raise WorkflowSchemaV2MigrationError()
        return named[0]
    if related:
        raise WorkflowSchemaV2MigrationError()
    return None


def _require_postgresql(target_engine) -> None:
    if target_engine.dialect.name != "postgresql":
        raise WorkflowSchemaV2MigrationError()


def _safe_schema_version_default(value) -> bool:
    if value is None:
        return True
    candidate = _strip_outer_parentheses(str(value).strip().lower())
    return re.fullmatch(
        r"1(?:::(?:(?:pg_catalog\.)?int4|integer))?",
        candidate,
    ) is not None


def _validate_schema_version_column(inspector, schema: str | None) -> None:
    columns = {
        column.get("name"): column
        for column in inspector.get_columns(TABLE, schema=schema)
    }
    column = columns.get("schema_version")
    if column is None:
        raise WorkflowSchemaV2MigrationError()
    actual_type = column.get("type")
    if (
        actual_type is None
        or actual_type.__class__.__name__.upper() != "INTEGER"
        or str(actual_type).upper() != "INTEGER"
    ):
        raise WorkflowSchemaV2MigrationError()
    if bool(column.get("nullable")):
        raise WorkflowSchemaV2MigrationError()
    if not _safe_schema_version_default(column.get("default")):
        raise WorkflowSchemaV2MigrationError()
    if column.get("identity") is not None or column.get("computed") is not None:
        raise WorkflowSchemaV2MigrationError()


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Converge only the named workflow-version check to exact versions 1/2."""

    target_engine = target_engine or engine
    _require_postgresql(target_engine)
    table_ref = _table(schema)
    try:
        with target_engine.begin() as connection:
            inspector = inspect(connection)
            if not inspector.has_table(TABLE, schema=schema):
                raise WorkflowSchemaV2MigrationError()
            _validate_schema_version_column(inspector, schema)
            current = _schema_version_constraint(inspector, schema)
            current_version = (
                _constraint_version(current.get("sqltext"))
                if current is not None
                else None
            )
            if current is not None and current_version is None:
                raise WorkflowSchemaV2MigrationError()
            if current_version == 2:
                return False
            if current_version == 1:
                connection.execute(text(
                    f"ALTER TABLE {table_ref} "
                    f"DROP CONSTRAINT {_quote(CONSTRAINT)}"
                ))
            connection.execute(text(
                f"ALTER TABLE {table_ref} "
                f"ADD CONSTRAINT {_quote(CONSTRAINT)} "
                f"CHECK ({V2_EXPRESSION})"
            ))
            final_inspector = inspect(connection)
            _validate_schema_version_column(final_inspector, schema)
            final = _schema_version_constraint(final_inspector, schema)
            if final is None or _constraint_version(final.get("sqltext")) != 2:
                raise WorkflowSchemaV2MigrationError()
        return True
    except WorkflowSchemaV2MigrationError:
        raise
    except Exception:
        raise WorkflowSchemaV2MigrationError() from None


def downgrade(target_engine=None, *, schema: str | None = None) -> bool:
    """Restore v1-only enforcement only when no v2 row exists."""

    target_engine = target_engine or engine
    _require_postgresql(target_engine)
    table_ref = _table(schema)
    try:
        with target_engine.begin() as connection:
            inspector = inspect(connection)
            if not inspector.has_table(TABLE, schema=schema):
                raise WorkflowSchemaV2MigrationError()
            _validate_schema_version_column(inspector, schema)
            current = _schema_version_constraint(inspector, schema)
            if current is None:
                raise WorkflowSchemaV2MigrationError()
            current_version = _constraint_version(current.get("sqltext"))
            if current_version == 1:
                return False
            if current_version != 2:
                raise WorkflowSchemaV2MigrationError()
            has_v2 = connection.execute(text(
                f"SELECT 1 FROM {table_ref} "
                "WHERE schema_version = 2 LIMIT 1"
            )).first()
            if has_v2 is not None:
                raise WorkflowSchemaV2MigrationError()
            connection.execute(text(
                f"ALTER TABLE {table_ref} "
                f"DROP CONSTRAINT {_quote(CONSTRAINT)}"
            ))
            connection.execute(text(
                f"ALTER TABLE {table_ref} "
                f"ADD CONSTRAINT {_quote(CONSTRAINT)} "
                f"CHECK ({V1_EXPRESSION})"
            ))
            final_inspector = inspect(connection)
            _validate_schema_version_column(final_inspector, schema)
            final = _schema_version_constraint(final_inspector, schema)
            if final is None or _constraint_version(final.get("sqltext")) != 1:
                raise WorkflowSchemaV2MigrationError()
        return True
    except WorkflowSchemaV2MigrationError:
        raise
    except Exception:
        raise WorkflowSchemaV2MigrationError() from None


if __name__ == "__main__":
    migrate()
