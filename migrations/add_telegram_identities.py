"""Add Telegram customer identities without changing customers or reservations."""

from pathlib import Path
import re
import sys

from sqlalchemy import DateTime, Integer, String, Uuid, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


USER_KEY_UNIQUE = "uq_telegram_identities_user_key"
CUSTOMER_UNIQUE = "uq_telegram_identities_customer_id"
CUSTOMER_FOREIGN_KEY = "fk_telegram_identities_customer_id_customers"
USER_KEY_INDEX = "ix_telegram_identities_user_key"
CUSTOMER_INDEX = "ix_telegram_identities_customer_id"
PRIMARY_KEY = "pk_telegram_identities"


class TelegramIdentityMigrationError(RuntimeError):
    """Sanitized fail-closed error for an incompatible existing schema."""


def _quote(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier supplied to migration.")
    return f'"{value}"'


def _table(schema: str | None, table: str) -> str:
    return f"{_quote(schema)}.{_quote(table)}" if schema else _quote(table)


def _default_text(column) -> str:
    return str(column.get("default") or "").strip().lower()


def _validate_existing_table(inspector, schema: str | None) -> None:
    expected = {
        "id": ("integer", False),
        "telegram_user_key": ("varchar", False),
        "customer_id": ("uuid", False),
        "is_active": ("boolean", False),
        "created_at": ("timestamptz", False),
        "updated_at": ("timestamptz", False),
    }
    columns = {
        column["name"]: column
        for column in inspector.get_columns("telegram_identities", schema=schema)
    }
    if set(columns) != set(expected):
        raise TelegramIdentityMigrationError("Existing Telegram identity columns are incompatible.")
    for name, (kind, nullable) in expected.items():
        actual = columns[name]["type"]
        compatible = (
            (kind == "integer" and isinstance(actual, Integer))
            or (kind == "varchar" and isinstance(actual, String) and actual.length == 64)
            or (kind == "uuid" and (isinstance(actual, Uuid) or actual.__class__.__name__.upper() == "UUID"))
            or (kind == "boolean" and actual.__class__.__name__.lower() == "boolean")
            or (kind == "timestamptz" and isinstance(actual, DateTime) and bool(actual.timezone))
        )
        if not compatible or bool(columns[name].get("nullable")) != nullable:
            raise TelegramIdentityMigrationError("Existing Telegram identity column definition is incompatible.")

    id_column = columns["id"]
    id_has_generator = bool(id_column.get("identity")) or "nextval(" in _default_text(id_column)
    if not id_has_generator:
        raise TelegramIdentityMigrationError("Telegram identity primary key generator is missing.")
    if "true" not in _default_text(columns["is_active"]):
        raise TelegramIdentityMigrationError("Telegram identity active default is incompatible.")
    for timestamp_name in ("created_at", "updated_at"):
        timestamp_default = _default_text(columns[timestamp_name])
        if "current_timestamp" not in timestamp_default and "now()" not in timestamp_default:
            raise TelegramIdentityMigrationError("Telegram identity timestamp default is incompatible.")

    primary_key = inspector.get_pk_constraint("telegram_identities", schema=schema)
    if primary_key.get("constrained_columns") != ["id"]:
        raise TelegramIdentityMigrationError("Existing Telegram identity primary key is incompatible.")


def _rename_constraint(connection, table_ref: str, current_name: str, expected_name: str) -> None:
    if not current_name:
        raise TelegramIdentityMigrationError("Existing Telegram constraint has no stable name.")
    connection.execute(text(
        f"ALTER TABLE {table_ref} RENAME CONSTRAINT {_quote(current_name)} TO {_quote(expected_name)}"
    ))


def _ensure_primary_key_name(connection, inspector, table_ref: str, schema: str | None) -> bool:
    primary_key = inspector.get_pk_constraint("telegram_identities", schema=schema)
    name = primary_key.get("name")
    if primary_key.get("constrained_columns") != ["id"]:
        raise TelegramIdentityMigrationError("Existing Telegram identity primary key is incompatible.")
    if name == PRIMARY_KEY:
        return False
    _rename_constraint(connection, table_ref, name, PRIMARY_KEY)
    return True


def _ensure_foreign_key(connection, inspector, table_ref: str, schema: str | None) -> bool:
    expected_schema = schema
    matching_name = None
    for foreign_key in inspector.get_foreign_keys("telegram_identities", schema=schema):
        if foreign_key.get("name") == CUSTOMER_FOREIGN_KEY:
            if (
                foreign_key.get("constrained_columns") != ["customer_id"]
                or foreign_key.get("referred_table") != "customers"
                or foreign_key.get("referred_columns") != ["id"]
                or foreign_key.get("referred_schema") != expected_schema
            ):
                raise TelegramIdentityMigrationError("Existing Telegram customer foreign key is incompatible.")
            return False
        if (
            foreign_key.get("constrained_columns") == ["customer_id"]
            and foreign_key.get("referred_table") == "customers"
            and foreign_key.get("referred_columns") == ["id"]
            and foreign_key.get("referred_schema") == expected_schema
        ):
            matching_name = foreign_key.get("name")
            continue
        if foreign_key.get("constrained_columns") == ["customer_id"]:
            raise TelegramIdentityMigrationError("Existing Telegram customer foreign key is ambiguous.")
    if matching_name:
        _rename_constraint(connection, table_ref, matching_name, CUSTOMER_FOREIGN_KEY)
        return True
    connection.execute(text(
        f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quote(CUSTOMER_FOREIGN_KEY)} "
        f"FOREIGN KEY (customer_id) REFERENCES {_table(schema, 'customers')} (id)"
    ))
    return True


def _ensure_unique(connection, inspector, table_ref: str, schema: str | None, name: str, column: str) -> bool:
    matching_name = None
    for constraint in inspector.get_unique_constraints("telegram_identities", schema=schema):
        if constraint.get("name") == name:
            if constraint.get("column_names") != [column]:
                raise TelegramIdentityMigrationError("Existing Telegram uniqueness constraint is incompatible.")
            return False
        if constraint.get("column_names") == [column]:
            matching_name = constraint.get("name")
    if matching_name:
        _rename_constraint(connection, table_ref, matching_name, name)
        return True
    for index in inspector.get_indexes("telegram_identities", schema=schema):
        if index.get("column_names") == [column] and index.get("unique"):
            raise TelegramIdentityMigrationError(
                "A unique index cannot replace the required named Telegram constraint."
            )
    connection.execute(text(
        f"ALTER TABLE {table_ref} ADD CONSTRAINT {_quote(name)} UNIQUE ({_quote(column)})"
    ))
    return True


def _ensure_index(connection, inspector, table_ref: str, schema: str | None, name: str, column: str) -> bool:
    for index in inspector.get_indexes("telegram_identities", schema=schema):
        if index.get("name") == name:
            if index.get("column_names") != [column] or bool(index.get("unique")):
                raise TelegramIdentityMigrationError("Existing Telegram lookup index is incompatible.")
            return False
    connection.execute(text(
        f"CREATE INDEX {_quote(name)} ON {table_ref} ({_quote(column)})"
    ))
    return True


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Create or converge only telegram_identities; return True on success.

    Idempotent convergence is a successful migration outcome even when no DDL
    is necessary. Incompatible schema or database failures raise safely.
    """
    target_engine = target_engine or engine
    table_ref = _table(schema, "telegram_identities")
    customers_ref = _table(schema, "customers")
    changed = False
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("customers", schema=schema):
            raise TelegramIdentityMigrationError("Required customers table is unavailable.")
        if not inspector.has_table("telegram_identities", schema=schema):
            connection.execute(text(f"""
                CREATE TABLE {table_ref} (
                    id SERIAL NOT NULL,
                    telegram_user_key VARCHAR(64) NOT NULL,
                    customer_id UUID NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT {_quote(PRIMARY_KEY)} PRIMARY KEY (id),
                    CONSTRAINT {_quote(USER_KEY_UNIQUE)} UNIQUE (telegram_user_key),
                    CONSTRAINT {_quote(CUSTOMER_UNIQUE)} UNIQUE (customer_id),
                    CONSTRAINT {_quote(CUSTOMER_FOREIGN_KEY)}
                        FOREIGN KEY (customer_id) REFERENCES {customers_ref} (id)
                )
            """))
            changed = True
            inspector = inspect(connection)
        else:
            _validate_existing_table(inspector, schema)
            changed |= _ensure_primary_key_name(connection, inspector, table_ref, schema)
            inspector = inspect(connection)
            changed |= _ensure_foreign_key(connection, inspector, table_ref, schema)
            inspector = inspect(connection)
            changed |= _ensure_unique(connection, inspector, table_ref, schema, USER_KEY_UNIQUE, "telegram_user_key")
            inspector = inspect(connection)
            changed |= _ensure_unique(connection, inspector, table_ref, schema, CUSTOMER_UNIQUE, "customer_id")
            inspector = inspect(connection)

        changed |= _ensure_index(connection, inspector, table_ref, schema, USER_KEY_INDEX, "telegram_user_key")
        inspector = inspect(connection)
        changed |= _ensure_index(connection, inspector, table_ref, schema, CUSTOMER_INDEX, "customer_id")
        # ``changed`` documents whether this invocation emitted DDL, not
        # whether it succeeded. A fully converged schema is successful.
        return True


if __name__ == "__main__":
    migrate()
