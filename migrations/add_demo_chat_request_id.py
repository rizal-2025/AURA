"""Add restart-safe request identity to isolated demo chat messages."""

from pathlib import Path
import re
import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


REQUEST_ID_COLUMN = "request_id"
REQUEST_ID_INDEXES = {
    "uq_demo_chat_messages_session_request_user": "role = 'user'",
    "uq_demo_chat_messages_session_request_assistant": (
        "role = 'assistant'"
    ),
}


class DemoChatRequestIdMigrationError(RuntimeError):
    def __init__(self):
        super().__init__("Demo chat request migration failed safely.")


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise DemoChatRequestIdMigrationError()
    return f'"{identifier}"'


def _table(schema: str | None) -> str:
    table = _quote("demo_chat_messages")
    return f"{_quote(schema)}.{table}" if schema else table


def _normalized_predicate(value) -> str:
    normalized = (
        str(value or "")
        .casefold()
        .replace("::text", "")
        .replace("::character varying", "")
        .replace('"', "")
    )
    return re.sub(r"[\s()]+", "", normalized)


def _predicate_is_compatible(value, *, role: str) -> bool:
    normalized = _normalized_predicate(value)
    return normalized in {
        f"role='{role}'andrequest_idisnotnull",
        f"request_idisnotnullandrole='{role}'",
    }


def _migrate(target_engine, *, schema: str | None) -> bool:
    changed = False
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("demo_chat_messages", schema=schema):
            raise DemoChatRequestIdMigrationError()

        columns = {
            item["name"]: item
            for item in inspector.get_columns(
                "demo_chat_messages",
                schema=schema,
            )
        }
        request_column = columns.get(REQUEST_ID_COLUMN)
        if request_column is None:
            connection.execute(
                text(
                    f"ALTER TABLE {_table(schema)} "
                    f"ADD COLUMN {_quote(REQUEST_ID_COLUMN)} UUID NULL"
                )
            )
            changed = True
        elif (
            request_column["type"].__class__.__name__.casefold() != "uuid"
            or not bool(request_column.get("nullable"))
        ):
            raise DemoChatRequestIdMigrationError()

        for name, predicate in REQUEST_ID_INDEXES.items():
            role = "user" if "user" in name else "assistant"
            inspector = inspect(connection)
            existing = next(
                (
                    item
                    for item in inspector.get_indexes(
                        "demo_chat_messages",
                        schema=schema,
                    )
                    if item.get("name") == name
                ),
                None,
            )
            if existing is not None:
                dialect_options = existing.get("dialect_options") or {}
                actual_predicate = dialect_options.get(
                    "postgresql_where",
                    "",
                )
                if (
                    tuple(existing.get("column_names") or ())
                    != ("demo_session_id", "request_id")
                    or not bool(existing.get("unique"))
                    or not _predicate_is_compatible(
                        actual_predicate,
                        role=role,
                    )
                ):
                    raise DemoChatRequestIdMigrationError()
                continue
            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX {_quote(name)} "
                    f"ON {_table(schema)} "
                    f"({_quote('demo_session_id')}, "
                    f"{_quote('request_id')}) "
                    f"WHERE {predicate} AND request_id IS NOT NULL"
                )
            )
            changed = True
    return changed


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    if target_engine is None:
        from app.db.database import engine as default_engine

        target_engine = default_engine
    try:
        return _migrate(target_engine, schema=schema)
    except DemoChatRequestIdMigrationError:
        raise
    except SQLAlchemyError:
        raise DemoChatRequestIdMigrationError() from None


if __name__ == "__main__":
    migrate()
