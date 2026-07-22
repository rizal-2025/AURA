"""Add the Phase E notification outbox without changing existing records."""

from pathlib import Path
import re
import sys

from sqlalchemy import BigInteger, DateTime, Integer, String, inspect, text
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


TABLE = "support_ticket_notifications"
PRIMARY_KEY = "pk_support_ticket_notifications"
FOREIGN_KEY = "fk_support_ticket_notifications_support_ticket_id"
UNIQUE_TICKET_CHANNEL = "uq_support_ticket_notifications_ticket_channel"
CHANNEL_CHECK = "ck_support_ticket_notifications_channel"
STATUS_CHECK = "ck_support_ticket_notifications_status"
ATTEMPT_CHECK = "ck_support_ticket_notifications_attempt_count"
DUE_INDEX = "ix_support_ticket_notifications_status_next_attempt"
LEASE_INDEX = "ix_support_ticket_notifications_status_lease"

EXPECTED_COLUMNS = {
    "id": ("integer", False),
    "support_ticket_id": ("integer", False),
    "channel": ("varchar:32", False),
    "status": ("varchar:16", False),
    "attempt_count": ("integer", False),
    "next_attempt_at": ("timestamptz", False),
    "lease_expires_at": ("timestamptz", True),
    "sent_at": ("timestamptz", True),
    "telegram_message_id": ("bigint", True),
    "last_error_code": ("varchar:32", True),
    "created_at": ("timestamptz", False),
    "updated_at": ("timestamptz", False),
}


class SupportTicketNotificationMigrationError(RuntimeError):
    pass


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise SupportTicketNotificationMigrationError("Invalid migration identifier.")
    return f'"{identifier}"'


def _table(schema: str | None, name: str) -> str:
    return f"{_quote(schema)}.{_quote(name)}" if schema else _quote(name)


def _compatible_type(actual, expected: str) -> bool:
    if expected == "integer":
        return isinstance(actual, Integer) and not isinstance(actual, BigInteger)
    if expected == "bigint":
        return isinstance(actual, BigInteger)
    if expected == "timestamptz":
        return isinstance(actual, DateTime) and bool(getattr(actual, "timezone", False))
    if expected.startswith("varchar:"):
        return isinstance(actual, String) and actual.length == int(expected.split(":")[1])
    return False


def _default(column) -> str:
    return " ".join(str(column.get("default") or "").lower().split())


def _validate_columns(inspector, schema: str | None) -> None:
    columns = {item["name"]: item for item in inspector.get_columns(TABLE, schema=schema)}
    if set(columns) != set(EXPECTED_COLUMNS):
        raise SupportTicketNotificationMigrationError("Existing notification columns are incompatible.")
    for name, (expected_type, nullable) in EXPECTED_COLUMNS.items():
        column = columns[name]
        if not _compatible_type(column["type"], expected_type) or bool(column.get("nullable")) != nullable:
            raise SupportTicketNotificationMigrationError("Existing notification column definition is incompatible.")
    if not columns["id"].get("identity") and "nextval(" not in _default(columns["id"]):
        raise SupportTicketNotificationMigrationError("Notification primary key generator is missing.")
    if "0" not in _default(columns["attempt_count"]):
        raise SupportTicketNotificationMigrationError("Notification attempt default is incompatible.")
    if "pending" not in _default(columns["status"]):
        raise SupportTicketNotificationMigrationError("Notification status default is incompatible.")
    if "telegram_owner" not in _default(columns["channel"]):
        raise SupportTicketNotificationMigrationError("Notification channel default is incompatible.")
    for name in ("next_attempt_at", "created_at", "updated_at"):
        if not any(value in _default(columns[name]) for value in ("current_timestamp", "now()")):
            raise SupportTicketNotificationMigrationError("Notification timestamp default is incompatible.")


def _rename_constraint(connection, table_ref: str, old: str, new: str) -> None:
    if not old:
        raise SupportTicketNotificationMigrationError("Unnamed constraint cannot be converged safely.")
    connection.execute(text(
        f"ALTER TABLE {table_ref} RENAME CONSTRAINT {_quote(old)} TO {_quote(new)}"
    ))


def _ensure_primary_key(connection, inspector, table_ref: str, schema: str | None) -> None:
    constraint = inspector.get_pk_constraint(TABLE, schema=schema)
    if constraint.get("constrained_columns") != ["id"]:
        raise SupportTicketNotificationMigrationError("Notification primary key is incompatible.")
    if constraint.get("name") != PRIMARY_KEY:
        _rename_constraint(connection, table_ref, constraint.get("name"), PRIMARY_KEY)


def _ensure_foreign_key(connection, inspector, table_ref: str, schema: str | None) -> None:
    matching = None
    for constraint in inspector.get_foreign_keys(TABLE, schema=schema):
        semantic = (
            constraint.get("constrained_columns") == ["support_ticket_id"]
            and constraint.get("referred_table") == "support_tickets"
            and constraint.get("referred_columns") == ["id"]
            and constraint.get("referred_schema") == schema
        )
        if constraint.get("name") == FOREIGN_KEY and not semantic:
            raise SupportTicketNotificationMigrationError("Notification foreign key is incompatible.")
        if constraint.get("constrained_columns") == ["support_ticket_id"] and not semantic:
            raise SupportTicketNotificationMigrationError("Notification foreign key target is incompatible.")
        if semantic:
            matching = constraint
    if matching is None:
        connection.execute(text(
            f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quote(FOREIGN_KEY)} "
            f"FOREIGN KEY (support_ticket_id) REFERENCES {_table(schema, 'support_tickets')} (id)"
        ))
    elif matching.get("name") != FOREIGN_KEY:
        _rename_constraint(connection, table_ref, matching.get("name"), FOREIGN_KEY)


def _ensure_unique(connection, inspector, table_ref: str, schema: str | None) -> None:
    matches = []
    for constraint in inspector.get_unique_constraints(TABLE, schema=schema):
        columns = constraint.get("column_names")
        if constraint.get("name") == UNIQUE_TICKET_CHANNEL and columns != ["support_ticket_id", "channel"]:
            raise SupportTicketNotificationMigrationError("Notification uniqueness is incompatible.")
        if columns == ["support_ticket_id", "channel"]:
            matches.append(constraint)
    if len(matches) > 1:
        raise SupportTicketNotificationMigrationError("Notification uniqueness is ambiguous.")
    matching = matches[0] if matches else None
    if matching is None:
        for index in inspector.get_indexes(TABLE, schema=schema):
            if index.get("column_names") == ["support_ticket_id", "channel"] and index.get("unique"):
                raise SupportTicketNotificationMigrationError("A unique index cannot replace the named constraint.")
        connection.execute(text(
            f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quote(UNIQUE_TICKET_CHANNEL)} "
            "UNIQUE (support_ticket_id, channel)"
        ))
    elif matching.get("name") != UNIQUE_TICKET_CHANNEL:
        _rename_constraint(connection, table_ref, matching.get("name"), UNIQUE_TICKET_CHANNEL)


def _check_matches(sqltext, semantic: str) -> bool:
    actual = " ".join(str(sqltext or "").lower().split())
    expected = semantic.lower()
    if "attempt_count" in expected:
        compact = re.sub(r"[\s()]|::integer", "", actual)
        return "attempt_count>=0" in compact and " or " not in actual and "true" not in actual
    column = expected.split(" ", 1)[0]
    expected_literals = set(re.findall(r"'([^']+)'", expected))
    actual_literals = set(re.findall(r"'([^']+)'", actual))
    return (
        column in actual
        and actual_literals == expected_literals
        and " or " not in actual
        and "true" not in actual
    )


def _ensure_check(connection, inspector, table_ref: str, schema: str | None, *, name: str, expression: str, semantic: str) -> None:
    matches = []
    for constraint in inspector.get_check_constraints(TABLE, schema=schema):
        is_semantic = _check_matches(constraint.get("sqltext"), semantic)
        if constraint.get("name") == name and not is_semantic:
            raise SupportTicketNotificationMigrationError("Notification check constraint is incompatible.")
        if is_semantic:
            matches.append(constraint)
    if len(matches) > 1:
        raise SupportTicketNotificationMigrationError("Notification check constraint is ambiguous.")
    matching = matches[0] if matches else None
    if matching is None:
        connection.execute(text(
            f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quote(name)} CHECK ({expression})"
        ))
    elif matching.get("name") != name:
        _rename_constraint(connection, table_ref, matching.get("name"), name)


def _ensure_index(connection, inspector, table_ref: str, schema: str | None, *, name: str, columns: tuple[str, ...]) -> None:
    existing = next((item for item in inspector.get_indexes(TABLE, schema=schema) if item.get("name") == name), None)
    if existing is not None:
        if tuple(existing.get("column_names") or ()) != columns or bool(existing.get("unique")):
            raise SupportTicketNotificationMigrationError("Notification lookup index is incompatible.")
        return
    rendered = ", ".join(_quote(column) for column in columns)
    connection.execute(text(f"CREATE INDEX {_quote(name)} ON {table_ref} ({rendered})"))


def _migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Create or converge only the notification outbox; convergence is success."""
    target_engine = target_engine or engine
    table_ref = _table(schema, TABLE)
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("support_tickets", schema=schema):
            raise SupportTicketNotificationMigrationError("Required support ticket table is unavailable.")
        if not inspector.has_table(TABLE, schema=schema):
            connection.execute(text(f"""
                CREATE TABLE {table_ref} (
                    id SERIAL NOT NULL,
                    support_ticket_id INTEGER NOT NULL,
                    channel VARCHAR(32) NOT NULL DEFAULT 'telegram_owner',
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    lease_expires_at TIMESTAMPTZ NULL,
                    sent_at TIMESTAMPTZ NULL,
                    telegram_message_id BIGINT NULL,
                    last_error_code VARCHAR(32) NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT {_quote(PRIMARY_KEY)} PRIMARY KEY (id),
                    CONSTRAINT {_quote(FOREIGN_KEY)} FOREIGN KEY (support_ticket_id)
                        REFERENCES {_table(schema, 'support_tickets')} (id),
                    CONSTRAINT {_quote(UNIQUE_TICKET_CHANNEL)} UNIQUE (support_ticket_id, channel),
                    CONSTRAINT {_quote(CHANNEL_CHECK)} CHECK (channel IN ('telegram_owner')),
                    CONSTRAINT {_quote(STATUS_CHECK)} CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
                    CONSTRAINT {_quote(ATTEMPT_CHECK)} CHECK (attempt_count >= 0)
                )
            """))

        inspector = inspect(connection)
        _validate_columns(inspector, schema)
        _ensure_primary_key(connection, inspector, table_ref, schema)
        inspector = inspect(connection)
        _ensure_foreign_key(connection, inspector, table_ref, schema)
        inspector = inspect(connection)
        _ensure_unique(connection, inspector, table_ref, schema)
        inspector = inspect(connection)
        _ensure_check(connection, inspector, table_ref, schema, name=CHANNEL_CHECK, expression="channel IN ('telegram_owner')", semantic="channel IN ('telegram_owner')")
        inspector = inspect(connection)
        _ensure_check(connection, inspector, table_ref, schema, name=STATUS_CHECK, expression="status IN ('pending', 'sending', 'sent', 'failed')", semantic="status IN ('pending', 'sending', 'sent', 'failed')")
        inspector = inspect(connection)
        _ensure_check(connection, inspector, table_ref, schema, name=ATTEMPT_CHECK, expression="attempt_count >= 0", semantic="attempt_count >= 0")
        inspector = inspect(connection)
        _ensure_index(connection, inspector, table_ref, schema, name=DUE_INDEX, columns=("status", "next_attempt_at"))
        inspector = inspect(connection)
        _ensure_index(connection, inspector, table_ref, schema, name=LEASE_INDEX, columns=("status", "lease_expires_at"))
    return True


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Run the transactional migration without exposing SQL or parameters."""
    try:
        return _migrate(target_engine, schema=schema)
    except SupportTicketNotificationMigrationError:
        raise
    except SQLAlchemyError:
        raise SupportTicketNotificationMigrationError(
            "Support ticket notification migration failed safely."
        ) from None


if __name__ == "__main__":
    migrate()
