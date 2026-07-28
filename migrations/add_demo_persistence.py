"""Add isolated demo persistence without changing core AURA tables."""

from pathlib import Path
import re
import sys

from sqlalchemy import DateTime, Integer, String, Text, Uuid, inspect, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEMO_TABLES = (
    "demo_sessions",
    "demo_chat_messages",
    "demo_handoff_events",
    "demo_rate_limit_buckets",
)

PRIMARY_KEYS = {
    "demo_sessions": ("pk_demo_sessions", ("id",)),
    "demo_chat_messages": ("pk_demo_chat_messages", ("id",)),
    "demo_handoff_events": ("pk_demo_handoff_events", ("id",)),
    "demo_rate_limit_buckets": ("pk_demo_rate_limit_buckets", ("id",)),
}

FOREIGN_KEYS = {
    "demo_sessions": (
        (
            "fk_demo_sessions_owner_customer_id_customers",
            ("owner_customer_id",),
            "customers",
            ("id",),
        ),
    ),
    "demo_chat_messages": (
        (
            "fk_demo_chat_messages_demo_session_id",
            ("demo_session_id",),
            "demo_sessions",
            ("id",),
        ),
    ),
    "demo_handoff_events": (
        (
            "fk_demo_handoff_events_demo_session_id",
            ("demo_session_id",),
            "demo_sessions",
            ("id",),
        ),
    ),
    "demo_rate_limit_buckets": (),
}

UNIQUE_CONSTRAINTS = {
    "demo_sessions": (
        ("uq_demo_sessions_token_digest", ("token_digest",)),
        (
            "uq_demo_sessions_owner_customer_id",
            ("owner_customer_id",),
        ),
    ),
    "demo_chat_messages": (),
    "demo_handoff_events": (
        ("uq_demo_handoff_events_reference", ("reference",)),
    ),
    "demo_rate_limit_buckets": (
        (
            "uq_demo_rate_limit_buckets_identity",
            (
                "scope_type",
                "subject_digest",
                "action",
                "window_started_at",
                "window_seconds",
            ),
        ),
    ),
}

CHECK_CONSTRAINTS = {
    "demo_sessions": (
        (
            "ck_demo_sessions_environment_scope",
            "environment_scope = 'demo'",
            ("environment_scope",),
        ),
        (
            "ck_demo_sessions_token_digest_length",
            "char_length(token_digest) = 64",
            ("token_digest", "64"),
        ),
        (
            "ck_demo_sessions_expiry_order",
            "idle_expires_at <= absolute_expires_at",
            ("idle_expires_at", "absolute_expires_at", "<="),
        ),
    ),
    "demo_chat_messages": (
        (
            "ck_demo_chat_messages_role",
            "role IN ('user', 'assistant')",
            ("role",),
        ),
    ),
    "demo_handoff_events": (
        (
            "ck_demo_handoff_events_status",
            "status = 'simulated'",
            ("status",),
        ),
        (
            "ck_demo_handoff_events_reference_prefix",
            "reference LIKE 'DEMO-HO-%'",
            ("reference",),
        ),
    ),
    "demo_rate_limit_buckets": (
        (
            "ck_demo_rate_limit_buckets_scope_type",
            "scope_type IN ('session', 'ip', 'global')",
            ("scope_type",),
        ),
        (
            "ck_demo_rate_limit_buckets_subject_digest_length",
            "char_length(subject_digest) = 64",
            ("subject_digest", "64"),
        ),
        (
            "ck_demo_rate_limit_buckets_window_seconds",
            "window_seconds > 0",
            ("window_seconds", "> 0"),
        ),
        (
            "ck_demo_rate_limit_buckets_request_count",
            "request_count >= 0",
            ("request_count", ">= 0"),
        ),
    ),
}

INDEXES = {
    "demo_sessions": (
        (
            "ix_demo_sessions_expiry",
            ("idle_expires_at", "absolute_expires_at"),
        ),
    ),
    "demo_chat_messages": (
        (
            "ix_demo_chat_messages_session_created",
            ("demo_session_id", "created_at", "id"),
        ),
    ),
    "demo_handoff_events": (
        (
            "ix_demo_handoff_events_session_created",
            ("demo_session_id", "created_at", "id"),
        ),
    ),
    "demo_rate_limit_buckets": (
        (
            "ix_demo_rate_limit_buckets_lookup",
            (
                "scope_type",
                "subject_digest",
                "action",
                "window_started_at",
            ),
        ),
        (
            "ix_demo_rate_limit_buckets_expires_at",
            ("expires_at",),
        ),
    ),
}

EXPECTED_COLUMNS = {
    "demo_sessions": {
        "id": ("integer", False),
        "token_digest": ("varchar:64", False),
        "owner_customer_id": ("uuid", False),
        "environment_scope": ("varchar:16", False),
        "created_at": ("timestamptz", False),
        "last_seen_at": ("timestamptz", False),
        "idle_expires_at": ("timestamptz", False),
        "absolute_expires_at": ("timestamptz", False),
        "revoked_at": ("timestamptz", True),
        "updated_at": ("timestamptz", False),
    },
    "demo_chat_messages": {
        "id": ("integer", False),
        "demo_session_id": ("integer", False),
        "role": ("varchar:16", False),
        "content": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "demo_handoff_events": {
        "id": ("integer", False),
        "demo_session_id": ("integer", False),
        "reference": ("varchar:64", False),
        "status": ("varchar:16", False),
        "reason_code": ("varchar:64", False),
        "safe_summary": ("text", True),
        "created_at": ("timestamptz", False),
    },
    "demo_rate_limit_buckets": {
        "id": ("integer", False),
        "scope_type": ("varchar:16", False),
        "subject_digest": ("varchar:64", False),
        "action": ("varchar:64", False),
        "window_started_at": ("timestamptz", False),
        "window_seconds": ("integer", False),
        "request_count": ("integer", False),
        "expires_at": ("timestamptz", False),
        "updated_at": ("timestamptz", False),
    },
}


class DemoPersistenceMigrationError(RuntimeError):
    """Safe migration failure without SQL or connection details."""

    def __init__(self, message: str = "Demo persistence migration failed safely."):
        super().__init__(message)


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise DemoPersistenceMigrationError("Invalid demo migration identifier.")
    return f'"{identifier}"'


def _table(schema: str | None, name: str) -> str:
    return f"{_quote(schema)}.{_quote(name)}" if schema else _quote(name)


def _compatible_type(actual, expected: str) -> bool:
    if expected == "integer":
        return isinstance(actual, Integer)
    if expected == "uuid":
        return (
            isinstance(actual, Uuid)
            or actual.__class__.__name__.upper() == "UUID"
        )
    if expected == "timestamptz":
        return isinstance(actual, DateTime) and bool(
            getattr(actual, "timezone", False)
        )
    if expected == "text":
        return isinstance(actual, Text) or actual.__class__.__name__.upper() == "TEXT"
    if expected.startswith("varchar:"):
        return (
            isinstance(actual, String)
            and getattr(actual, "length", None)
            == int(expected.split(":", 1)[1])
        )
    return False


def _validate_columns(inspector, table_name: str, schema: str | None) -> None:
    columns = {
        item["name"]: item
        for item in inspector.get_columns(table_name, schema=schema)
    }
    expected = EXPECTED_COLUMNS[table_name]
    if set(columns) != set(expected):
        raise DemoPersistenceMigrationError(
            f"Existing {table_name} columns are incompatible."
        )
    for name, (expected_type, nullable) in expected.items():
        column = columns[name]
        if (
            not _compatible_type(column["type"], expected_type)
            or bool(column.get("nullable")) != nullable
        ):
            raise DemoPersistenceMigrationError(
                f"Existing {table_name}.{name} definition is incompatible."
            )
    id_column = columns["id"]
    default = str(id_column.get("default") or "").casefold()
    if not id_column.get("identity") and "nextval(" not in default:
        raise DemoPersistenceMigrationError(
            f"Existing {table_name}.id generator is missing."
        )


def _validate_primary_key(
    inspector,
    table_name: str,
    schema: str | None,
) -> None:
    expected_name, expected_columns = PRIMARY_KEYS[table_name]
    primary_key = inspector.get_pk_constraint(table_name, schema=schema)
    if (
        primary_key.get("name") != expected_name
        or tuple(primary_key.get("constrained_columns") or ())
        != expected_columns
    ):
        raise DemoPersistenceMigrationError(
            f"Existing {table_name} primary key is incompatible."
        )


def _ensure_foreign_key(
    connection,
    inspector,
    *,
    table_name: str,
    schema: str | None,
    name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
) -> bool:
    matching = None
    expected_schemas = {schema, None}
    for item in inspector.get_foreign_keys(table_name, schema=schema):
        semantic = (
            tuple(item.get("constrained_columns") or ()) == columns
            and item.get("referred_table") == referred_table
            and tuple(item.get("referred_columns") or ()) == referred_columns
            and item.get("referred_schema") in expected_schemas
            and not (item.get("options") or {}).get("ondelete")
        )
        if item.get("name") == name and not semantic:
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} foreign key is incompatible."
            )
        if semantic:
            matching = item
    if matching is not None:
        if matching.get("name") != name:
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} foreign key name is incompatible."
            )
        return False
    rendered_columns = ", ".join(_quote(column) for column in columns)
    rendered_referred = ", ".join(
        _quote(column) for column in referred_columns
    )
    connection.execute(text(
        f"ALTER TABLE {_table(schema, table_name)} "
        f"ADD CONSTRAINT {_quote(name)} "
        f"FOREIGN KEY ({rendered_columns}) "
        f"REFERENCES {_table(schema, referred_table)} "
        f"({rendered_referred})"
    ))
    return True


def _ensure_unique(
    connection,
    inspector,
    *,
    table_name: str,
    schema: str | None,
    name: str,
    columns: tuple[str, ...],
) -> bool:
    matching = None
    for item in inspector.get_unique_constraints(table_name, schema=schema):
        semantic = tuple(item.get("column_names") or ()) == columns
        if item.get("name") == name and not semantic:
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} uniqueness is incompatible."
            )
        if semantic:
            matching = item
    if matching is not None:
        if matching.get("name") != name:
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} unique name is incompatible."
            )
        return False
    rendered = ", ".join(_quote(column) for column in columns)
    connection.execute(text(
        f"ALTER TABLE {_table(schema, table_name)} "
        f"ADD CONSTRAINT {_quote(name)} UNIQUE ({rendered})"
    ))
    return True


def _normalized_check(value) -> str:
    return " ".join(
        str(value or "")
        .casefold()
        .replace("::text", "")
        .replace("::character varying", "")
        .split()
    )


def _check_is_compatible(
    actual,
    *,
    expression: str,
    required_fragments: tuple[str, ...],
) -> bool:
    normalized = _normalized_check(actual)
    expected_literals = set(re.findall(r"'([^']+)'", expression.casefold()))
    actual_literals = set(re.findall(r"'([^']+)'", normalized))
    if expected_literals != actual_literals:
        return False
    compact = re.sub(r"\s+", "", normalized)
    return all(
        fragment.casefold().replace(" ", "") in compact
        for fragment in required_fragments
    ) and " or " not in normalized and "true" not in normalized


def _ensure_check(
    connection,
    inspector,
    *,
    table_name: str,
    schema: str | None,
    name: str,
    expression: str,
    required_fragments: tuple[str, ...],
) -> bool:
    existing = {
        item.get("name"): item
        for item in inspector.get_check_constraints(
            table_name,
            schema=schema,
        )
    }
    constraint = existing.get(name)
    if constraint is not None:
        if not _check_is_compatible(
            constraint.get("sqltext"),
            expression=expression,
            required_fragments=required_fragments,
        ):
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} check is incompatible."
            )
        return False
    connection.execute(text(
        f"ALTER TABLE {_table(schema, table_name)} "
        f"ADD CONSTRAINT {_quote(name)} CHECK ({expression})"
    ))
    return True


def _ensure_index(
    connection,
    inspector,
    *,
    table_name: str,
    schema: str | None,
    name: str,
    columns: tuple[str, ...],
) -> bool:
    existing = next(
        (
            item
            for item in inspector.get_indexes(table_name, schema=schema)
            if item.get("name") == name
        ),
        None,
    )
    if existing is not None:
        if (
            tuple(existing.get("column_names") or ()) != columns
            or bool(existing.get("unique"))
        ):
            raise DemoPersistenceMigrationError(
                f"Existing {table_name} index is incompatible."
            )
        return False
    rendered = ", ".join(_quote(column) for column in columns)
    connection.execute(text(
        f"CREATE INDEX {_quote(name)} "
        f"ON {_table(schema, table_name)} ({rendered})"
    ))
    return True


def _create_tables(connection, *, schema: str | None) -> bool:
    inspector = inspect(connection)
    if not inspector.has_table("customers", schema=schema):
        raise DemoPersistenceMigrationError(
            "Required customers table is unavailable."
        )
    changed = False
    customers = _table(schema, "customers")
    sessions = _table(schema, "demo_sessions")

    if not inspector.has_table("demo_sessions", schema=schema):
        connection.execute(text(f"""
            CREATE TABLE {sessions} (
                id SERIAL NOT NULL,
                token_digest VARCHAR(64) NOT NULL,
                owner_customer_id UUID NOT NULL,
                environment_scope VARCHAR(16) NOT NULL DEFAULT 'demo',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                idle_expires_at TIMESTAMPTZ NOT NULL,
                absolute_expires_at TIMESTAMPTZ NOT NULL,
                revoked_at TIMESTAMPTZ NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT {_quote('pk_demo_sessions')} PRIMARY KEY (id),
                CONSTRAINT {_quote('uq_demo_sessions_token_digest')}
                    UNIQUE (token_digest),
                CONSTRAINT {_quote('uq_demo_sessions_owner_customer_id')}
                    UNIQUE (owner_customer_id),
                CONSTRAINT {_quote(
                    'fk_demo_sessions_owner_customer_id_customers'
                )} FOREIGN KEY (owner_customer_id)
                    REFERENCES {customers} (id),
                CONSTRAINT {_quote('ck_demo_sessions_environment_scope')}
                    CHECK (environment_scope = 'demo'),
                CONSTRAINT {_quote('ck_demo_sessions_token_digest_length')}
                    CHECK (char_length(token_digest) = 64),
                CONSTRAINT {_quote('ck_demo_sessions_expiry_order')}
                    CHECK (idle_expires_at <= absolute_expires_at)
            )
        """))
        changed = True

    inspector = inspect(connection)
    if not inspector.has_table("demo_chat_messages", schema=schema):
        connection.execute(text(f"""
            CREATE TABLE {_table(schema, 'demo_chat_messages')} (
                id SERIAL NOT NULL,
                demo_session_id INTEGER NOT NULL,
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT {_quote('pk_demo_chat_messages')} PRIMARY KEY (id),
                CONSTRAINT {_quote(
                    'fk_demo_chat_messages_demo_session_id'
                )} FOREIGN KEY (demo_session_id)
                    REFERENCES {sessions} (id),
                CONSTRAINT {_quote('ck_demo_chat_messages_role')}
                    CHECK (role IN ('user', 'assistant'))
            )
        """))
        changed = True

    inspector = inspect(connection)
    if not inspector.has_table("demo_handoff_events", schema=schema):
        connection.execute(text(f"""
            CREATE TABLE {_table(schema, 'demo_handoff_events')} (
                id SERIAL NOT NULL,
                demo_session_id INTEGER NOT NULL,
                reference VARCHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'simulated',
                reason_code VARCHAR(64) NOT NULL,
                safe_summary TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT {_quote('pk_demo_handoff_events')} PRIMARY KEY (id),
                CONSTRAINT {_quote('uq_demo_handoff_events_reference')}
                    UNIQUE (reference),
                CONSTRAINT {_quote(
                    'fk_demo_handoff_events_demo_session_id'
                )} FOREIGN KEY (demo_session_id)
                    REFERENCES {sessions} (id),
                CONSTRAINT {_quote('ck_demo_handoff_events_status')}
                    CHECK (status = 'simulated'),
                CONSTRAINT {_quote(
                    'ck_demo_handoff_events_reference_prefix'
                )} CHECK (reference LIKE 'DEMO-HO-%')
            )
        """))
        changed = True

    inspector = inspect(connection)
    if not inspector.has_table("demo_rate_limit_buckets", schema=schema):
        connection.execute(text(f"""
            CREATE TABLE {_table(schema, 'demo_rate_limit_buckets')} (
                id SERIAL NOT NULL,
                scope_type VARCHAR(16) NOT NULL,
                subject_digest VARCHAR(64) NOT NULL,
                action VARCHAR(64) NOT NULL,
                window_started_at TIMESTAMPTZ NOT NULL,
                window_seconds INTEGER NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                expires_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT {_quote('pk_demo_rate_limit_buckets')}
                    PRIMARY KEY (id),
                CONSTRAINT {_quote(
                    'uq_demo_rate_limit_buckets_identity'
                )} UNIQUE (
                    scope_type,
                    subject_digest,
                    action,
                    window_started_at,
                    window_seconds
                ),
                CONSTRAINT {_quote(
                    'ck_demo_rate_limit_buckets_scope_type'
                )} CHECK (scope_type IN ('session', 'ip', 'global')),
                CONSTRAINT {_quote(
                    'ck_demo_rate_limit_buckets_subject_digest_length'
                )} CHECK (char_length(subject_digest) = 64),
                CONSTRAINT {_quote(
                    'ck_demo_rate_limit_buckets_window_seconds'
                )} CHECK (window_seconds > 0),
                CONSTRAINT {_quote(
                    'ck_demo_rate_limit_buckets_request_count'
                )} CHECK (request_count >= 0)
            )
        """))
        changed = True
    return changed


def _migrate(target_engine, *, schema: str | None) -> bool:
    changed = False
    with target_engine.begin() as connection:
        changed |= _create_tables(connection, schema=schema)
        for table_name in DEMO_TABLES:
            inspector = inspect(connection)
            _validate_columns(inspector, table_name, schema)
            _validate_primary_key(inspector, table_name, schema)

            for name, columns, referred_table, referred_columns in FOREIGN_KEYS[
                table_name
            ]:
                inspector = inspect(connection)
                changed |= _ensure_foreign_key(
                    connection,
                    inspector,
                    table_name=table_name,
                    schema=schema,
                    name=name,
                    columns=columns,
                    referred_table=referred_table,
                    referred_columns=referred_columns,
                )

            for name, columns in UNIQUE_CONSTRAINTS[table_name]:
                inspector = inspect(connection)
                changed |= _ensure_unique(
                    connection,
                    inspector,
                    table_name=table_name,
                    schema=schema,
                    name=name,
                    columns=columns,
                )

            for name, expression, required_fragments in CHECK_CONSTRAINTS[
                table_name
            ]:
                inspector = inspect(connection)
                changed |= _ensure_check(
                    connection,
                    inspector,
                    table_name=table_name,
                    schema=schema,
                    name=name,
                    expression=expression,
                    required_fragments=required_fragments,
                )

            for name, columns in INDEXES[table_name]:
                inspector = inspect(connection)
                changed |= _ensure_index(
                    connection,
                    inspector,
                    table_name=table_name,
                    schema=schema,
                    name=name,
                    columns=columns,
                )
    return changed


def migrate(target_engine=None, *, schema: str | None = None) -> bool:
    """Create or converge the four demo tables in one transaction."""
    if target_engine is None:
        from app.db.database import engine as default_engine

        target_engine = default_engine
    try:
        return _migrate(target_engine, schema=schema)
    except DemoPersistenceMigrationError:
        raise
    except SQLAlchemyError:
        raise DemoPersistenceMigrationError() from None


if __name__ == "__main__":
    migrate()
