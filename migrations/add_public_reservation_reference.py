"""Add and backfill immutable public-safe reservation references."""

from __future__ import annotations

from pathlib import Path
import re
import secrets
import sys

from sqlalchemy import String, inspect, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TABLE = "reservations"
COLUMN = "public_reference"
REFERENCE_LENGTH = 36
ENTROPY_BYTES = 16
MAX_ATTEMPTS = 5
BATCH_SIZE = 500
FORMAT_PATTERN = r"^RSV_[0-9a-f]{32}$"
FORMAT_CONSTRAINT = "ck_reservations_public_reference_format"
UNIQUE_CONSTRAINT = "uq_reservations_public_reference"


class PublicReservationReferenceMigrationError(RuntimeError):
    """Stable migration failure without SQL, identifiers, or row values."""

    code = "PUBLIC_RESERVATION_REFERENCE_MIGRATION_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise PublicReservationReferenceMigrationError()
    return f'"{identifier}"'


def _table(schema: str | None) -> str:
    table = _quote(TABLE)
    return f"{_quote(schema)}.{table}" if schema else table


def _reference() -> str:
    return "RSV_" + secrets.token_hex(ENTROPY_BYTES)


def _column_is_compatible(column) -> bool:
    return (
        isinstance(column["type"], String)
        and getattr(column["type"], "length", None) == REFERENCE_LENGTH
        and bool(column.get("nullable"))
    )


_FORMAT_IDENTIFIER = rf'(?:"{re.escape(COLUMN)}"|{re.escape(COLUMN)})'
_SAFE_TEXT_CAST = r'(?:::\s*(?i:text|character\s+varying))?'
_FORMAT_LITERAL = re.escape(f"'{FORMAT_PATTERN}'")
_EXACT_FORMAT_SQLTEXT = re.compile(
    rf"\s*{_FORMAT_IDENTIFIER}{_SAFE_TEXT_CAST}\s+"
    rf"(?i:IS)\s+(?i:NULL)\s+(?i:OR)\s+"
    rf"{_FORMAT_IDENTIFIER}{_SAFE_TEXT_CAST}\s*~\s*"
    rf"{_FORMAT_LITERAL}{_SAFE_TEXT_CAST}\s*"
)


def _check_is_exact(sqltext) -> bool:
    """Accept only the migration-owned, exact fail-closed check grammar."""

    if not isinstance(sqltext, str):
        return False
    return _EXACT_FORMAT_SQLTEXT.fullmatch(sqltext) is not None


def _rename_constraint(
    connection,
    table_ref: str,
    old_name: str | None,
    new_name: str,
) -> None:
    if not old_name:
        raise PublicReservationReferenceMigrationError()
    connection.execute(
        text(
            f"ALTER TABLE {table_ref} "
            f"RENAME CONSTRAINT {_quote(old_name)} TO {_quote(new_name)}"
        )
    )


def _ensure_format_constraint(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
) -> bool:
    named_constraint = next(
        (
            constraint
            for constraint in inspector.get_check_constraints(
                TABLE,
                schema=schema,
            )
            if constraint.get("name") == FORMAT_CONSTRAINT
        ),
        None,
    )
    if named_constraint is None:
        connection.execute(
            text(
                f"ALTER TABLE {table_ref} "
                f"ADD CONSTRAINT {_quote(FORMAT_CONSTRAINT)} "
                f"CHECK ({_quote(COLUMN)} IS NULL OR "
                f"{_quote(COLUMN)} ~ '{FORMAT_PATTERN}')"
            )
        )
        return True
    # This constraint is a security boundary. Never infer equivalence from an
    # unnamed or merely similar expression, and never replace weak DDL silently.
    if not _check_is_exact(named_constraint.get("sqltext")):
        raise PublicReservationReferenceMigrationError()
    return False


def _ensure_unique_constraint(
    connection,
    inspector,
    table_ref: str,
    schema: str | None,
) -> bool:
    matches = []
    for constraint in inspector.get_unique_constraints(TABLE, schema=schema):
        columns = constraint.get("column_names")
        if constraint.get("name") == UNIQUE_CONSTRAINT and columns != [COLUMN]:
            raise PublicReservationReferenceMigrationError()
        if columns == [COLUMN]:
            matches.append(constraint)

    if len(matches) > 1:
        raise PublicReservationReferenceMigrationError()
    if not matches:
        conflicting_index = next(
            (
                item
                for item in inspector.get_indexes(TABLE, schema=schema)
                if item.get("name") == UNIQUE_CONSTRAINT
                or (
                    item.get("column_names") == [COLUMN]
                    and bool(item.get("unique"))
                )
            ),
            None,
        )
        if conflicting_index is not None:
            raise PublicReservationReferenceMigrationError()
        connection.execute(
            text(
                f"ALTER TABLE {table_ref} "
                f"ADD CONSTRAINT {_quote(UNIQUE_CONSTRAINT)} "
                f"UNIQUE ({_quote(COLUMN)})"
            )
        )
        return True
    if matches[0].get("name") != UNIQUE_CONSTRAINT:
        _rename_constraint(
            connection,
            table_ref,
            matches[0].get("name"),
            UNIQUE_CONSTRAINT,
        )
        return True
    return False


def _reference_exists(connection, table_ref: str, reference: str) -> bool:
    return (
        connection.execute(
            text(
                f"SELECT 1 FROM {table_ref} "
                f"WHERE {_quote(COLUMN)} = :reference LIMIT 1"
            ),
            {"reference": reference},
        ).first()
        is not None
    )


def _next_unique_reference(connection, table_ref: str) -> str:
    for _attempt in range(MAX_ATTEMPTS):
        candidate = _reference()
        if (
            re.fullmatch(FORMAT_PATTERN, candidate) is not None
            and not _reference_exists(connection, table_ref, candidate)
        ):
            return candidate
    raise PublicReservationReferenceMigrationError()


def _backfill(connection, table_ref: str) -> bool:
    changed = False
    while True:
        row_ids = list(
            connection.execute(
                text(
                    f"SELECT id FROM {table_ref} "
                    f"WHERE {_quote(COLUMN)} IS NULL "
                    "ORDER BY id ASC LIMIT :batch_size"
                ),
                {"batch_size": BATCH_SIZE},
            ).scalars()
        )
        if not row_ids:
            return changed

        for row_id in row_ids:
            reference = _next_unique_reference(connection, table_ref)
            result = connection.execute(
                text(
                    f"UPDATE {table_ref} "
                    f"SET {_quote(COLUMN)} = :reference "
                    f"WHERE id = :row_id AND {_quote(COLUMN)} IS NULL"
                ),
                {"reference": reference, "row_id": row_id},
            )
            if result.rowcount != 1:
                raise PublicReservationReferenceMigrationError()
            changed = True


def _migrate(target_engine, *, schema: str | None) -> bool:
    if target_engine.dialect.name != "postgresql":
        raise PublicReservationReferenceMigrationError()

    table_ref = _table(schema)
    changed = False
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(TABLE, schema=schema):
            raise PublicReservationReferenceMigrationError()

        columns = {
            item["name"]: item
            for item in inspector.get_columns(TABLE, schema=schema)
        }
        existing = columns.get(COLUMN)
        if existing is None:
            connection.execute(
                text(
                    f"ALTER TABLE {table_ref} "
                    f"ADD COLUMN {_quote(COLUMN)} "
                    f"VARCHAR({REFERENCE_LENGTH}) NULL"
                )
            )
            changed = True
        elif not _column_is_compatible(existing):
            raise PublicReservationReferenceMigrationError()

        inspector = inspect(connection)
        changed |= _ensure_format_constraint(
            connection,
            inspector,
            table_ref,
            schema,
        )
        changed |= _backfill(connection, table_ref)

        inspector = inspect(connection)
        changed |= _ensure_unique_constraint(
            connection,
            inspector,
            table_ref,
            schema,
        )

        inspector = inspect(connection)
        migrated_column = next(
            (
                item
                for item in inspector.get_columns(TABLE, schema=schema)
                if item["name"] == COLUMN
            ),
            None,
        )
        if migrated_column is None or not _column_is_compatible(migrated_column):
            raise PublicReservationReferenceMigrationError()

        migrated_format = next(
            (
                item
                for item in inspector.get_check_constraints(TABLE, schema=schema)
                if item.get("name") == FORMAT_CONSTRAINT
            ),
            None,
        )
        if migrated_format is None or not _check_is_exact(
            migrated_format.get("sqltext")
        ):
            raise PublicReservationReferenceMigrationError()

        migrated_unique = next(
            (
                item
                for item in inspector.get_unique_constraints(TABLE, schema=schema)
                if item.get("name") == UNIQUE_CONSTRAINT
            ),
            None,
        )
        if (
            migrated_unique is None
            or migrated_unique.get("column_names") != [COLUMN]
        ):
            raise PublicReservationReferenceMigrationError()

    return changed


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Convergently add public references without exposing migration details."""

    if target_engine is None:
        from app.db.database import engine as default_engine

        target_engine = default_engine
    try:
        return _migrate(target_engine, schema=schema)
    except PublicReservationReferenceMigrationError:
        raise
    except SQLAlchemyError:
        raise PublicReservationReferenceMigrationError() from None


def downgrade(*_args, **_kwargs) -> None:
    """Refuse destructive removal of references that may have been issued."""

    raise PublicReservationReferenceMigrationError()


if __name__ == "__main__":
    migrate()
