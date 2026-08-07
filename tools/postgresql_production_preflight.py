"""Secret-safe PostgreSQL preflight for the Windows production credential."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


EXPECTED_DATABASE = "aura_demo_public"
EXPECTED_USER = "aura_public_runtime"
EXPECTED_OWNER = "aura_migration_owner"
EXPECTED_HOSTS = frozenset({"127.0.0.1", "localhost"})
EXPECTED_PORT = 5432
EXPECTED_DRIVER = "postgresql+psycopg"


class PostgreSQLProductionPreflightError(RuntimeError):
    """A stable error whose text never includes configuration or driver data."""


def build_production_database_url() -> URL:
    """Build the fixed password-free URL; libpq obtains auth from PGPASSFILE."""
    return URL.create(
        EXPECTED_DRIVER,
        username=EXPECTED_USER,
        host="127.0.0.1",
        port=EXPECTED_PORT,
        database=EXPECTED_DATABASE,
    )


def validate_production_database_url(value: object) -> URL:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_URL_INVALID"
        )
    try:
        parsed = make_url(value)
        host = (parsed.host or "").casefold()
        port = parsed.port
    except (ArgumentError, AttributeError, TypeError, ValueError):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_URL_INVALID"
        ) from None
    if (
        parsed.drivername.casefold() != EXPECTED_DRIVER
        or host not in EXPECTED_HOSTS
        or port != EXPECTED_PORT
        or parsed.database != EXPECTED_DATABASE
        or parsed.username != EXPECTED_USER
        or parsed.password is not None
        or parsed.query
    ):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_TARGET_INVALID"
        )
    return parsed


def _database_access(connection) -> dict[str, bool]:
    rows = connection.execute(
        text(
            "SELECT datname, has_database_privilege("
            "current_user, datname, 'CONNECT') FROM pg_database "
            "WHERE datname IN ('aura_test', 'aura_demo_staging')"
        )
    ).all()
    return {str(database): bool(allowed) for database, allowed in rows}


def run_preflight() -> None:
    value = os.environ.get("DEMO_DATABASE_URL")
    print(f"variable present: {'yes' if value else 'no'}")
    print(
        "APP_ENV is demo: "
        f"{'yes' if os.environ.get('APP_ENV') == 'demo' else 'no'}"
    )
    if os.environ.get("APP_ENV") != "demo":
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_APP_ENV_INVALID"
        )
    if not os.environ.get("PGPASSFILE"):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_PGPASSFILE_MISSING"
        )

    parsed = validate_production_database_url(value)
    print("parse success: yes")
    print("password present: no")

    engine = None
    try:
        engine = create_engine(
            parsed,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as connection:
            current_database, current_user = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
            role = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, "
                    "rolreplication, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            membership_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_auth_members WHERE member = "
                    "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
                )
            ).scalar_one()
            database_owner = connection.execute(
                text(
                    "SELECT pg_get_userbyid(datdba) FROM pg_database "
                    "WHERE datname = current_database()"
                )
            ).scalar_one()
            privileges = connection.execute(
                text(
                    "SELECT "
                    "has_database_privilege(current_user, current_database(), 'CONNECT'), "
                    "has_database_privilege(current_user, current_database(), 'TEMPORARY'), "
                    "has_database_privilege(current_user, current_database(), 'CREATE'), "
                    "has_schema_privilege(current_user, 'public', 'USAGE'), "
                    "has_schema_privilege(current_user, 'public', 'CREATE')"
                )
            ).one()
            table_privileges = connection.execute(
                text(
                    "SELECT "
                    "coalesce(bool_and(has_table_privilege(current_user, c.oid, "
                    "'SELECT,INSERT,UPDATE,DELETE')), true), "
                    "coalesce(bool_or(has_table_privilege(current_user, c.oid, "
                    "'TRUNCATE,REFERENCES,TRIGGER')), false) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')"
                )
            ).one()
            sequence_privileges = connection.execute(
                text(
                    "SELECT coalesce(bool_and(has_sequence_privilege("
                    "current_user, c.oid, 'USAGE,SELECT,UPDATE')), true) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'S'"
                )
            ).scalar_one()
            forbidden_access = _database_access(connection)
    except Exception:
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_CONNECTION_FAILED"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()

    print("connectivity success: yes")
    if current_database != EXPECTED_DATABASE or current_user != EXPECTED_USER:
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_IDENTITY_INVALID"
        )
    if any(role) or membership_count != 0:
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_ROLE_PRIVILEGES_INVALID"
        )
    if database_owner != EXPECTED_OWNER:
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_DATABASE_OWNER_INVALID"
        )
    if tuple(bool(item) for item in privileges) != (
        True,
        False,
        False,
        True,
        False,
    ):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_ROLE_PRIVILEGES_INVALID"
        )
    if tuple(bool(item) for item in table_privileges) != (True, False):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_TABLE_PRIVILEGES_INVALID"
        )
    if not sequence_privileges:
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_SEQUENCE_PRIVILEGES_INVALID"
        )
    if any(forbidden_access.values()):
        raise PostgreSQLProductionPreflightError(
            "AURA_PRODUCTION_CROSS_DATABASE_ACCESS"
        )
    print("least-privilege role checks: passed")
    print("test/staging database access: rejected")
    print("AURA_POSTGRESQL_PRODUCTION_PREFLIGHT_OK")


def main() -> int:
    try:
        run_preflight()
    except PostgreSQLProductionPreflightError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("AURA_PRODUCTION_PREFLIGHT_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
