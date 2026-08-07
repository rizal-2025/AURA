"""Secret-safe local PostgreSQL preflight for the guarded unittest runner."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


EXPECTED_DATABASE = "aura_test"
EXPECTED_USER = "aura_test_runner"
EXPECTED_OWNER = "aura_migration_owner"
EXPECTED_HOSTS = frozenset({"127.0.0.1", "localhost"})
EXPECTED_PORT = 5432
EXPECTED_DRIVER = "postgresql+psycopg"


class PostgreSQLTestPreflightError(RuntimeError):
    """A stable error whose text never includes configuration or driver data."""


def build_test_database_url() -> URL:
    """Build the fixed password-free URL; libpq obtains auth from PGPASSFILE."""
    return URL.create(
        EXPECTED_DRIVER,
        username=EXPECTED_USER,
        host="127.0.0.1",
        port=EXPECTED_PORT,
        database=EXPECTED_DATABASE,
    )


def validate_test_database_url(value: object) -> URL:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PostgreSQLTestPreflightError("AURA_TEST_DATABASE_URL_INVALID")
    try:
        parsed = make_url(value)
        host = (parsed.host or "").casefold()
        port = parsed.port
    except (ArgumentError, AttributeError, TypeError, ValueError):
        raise PostgreSQLTestPreflightError(
            "AURA_TEST_DATABASE_URL_INVALID"
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
        raise PostgreSQLTestPreflightError("AURA_TEST_DATABASE_TARGET_INVALID")
    return parsed


def _database_access(connection) -> dict[str, bool]:
    rows = connection.execute(
        text(
            "SELECT datname, has_database_privilege("
            "current_user, datname, 'CONNECT') FROM pg_database "
            "WHERE datname IN ('aura_demo_public', 'aura_demo_staging')"
        )
    ).all()
    return {str(database): bool(allowed) for database, allowed in rows}


def run_preflight() -> None:
    value = os.environ.get("TEST_DATABASE_URL")
    print(f"variable present: {'yes' if value else 'no'}")
    print(
        "APP_ENV is test: "
        f"{'yes' if os.environ.get('APP_ENV') == 'test' else 'no'}"
    )
    if os.environ.get("APP_ENV") != "test":
        raise PostgreSQLTestPreflightError("AURA_TEST_APP_ENV_INVALID")
    if not os.environ.get("PGPASSFILE"):
        raise PostgreSQLTestPreflightError("AURA_TEST_PGPASSFILE_MISSING")

    parsed = validate_test_database_url(value)
    print("parse success: yes")
    print(f"SQLAlchemy backend: {parsed.get_backend_name()}")
    print(f"host: {parsed.host}")
    print(f"port: {parsed.port}")
    print(f"database: {parsed.database}")
    print(f"username: {parsed.username}")
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
            forbidden_access = _database_access(connection)
    except Exception:
        raise PostgreSQLTestPreflightError(
            "AURA_TEST_DATABASE_CONNECTION_FAILED"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()

    print("connectivity success: yes")
    print(f"current_database(): {current_database}")
    print(f"current_user: {current_user}")
    if current_database != EXPECTED_DATABASE or current_user != EXPECTED_USER:
        raise PostgreSQLTestPreflightError("AURA_TEST_DATABASE_IDENTITY_INVALID")
    if any(role) or membership_count != 0:
        raise PostgreSQLTestPreflightError("AURA_TEST_ROLE_PRIVILEGES_INVALID")
    if database_owner != EXPECTED_OWNER:
        raise PostgreSQLTestPreflightError("AURA_TEST_DATABASE_OWNER_INVALID")
    if tuple(bool(value) for value in privileges) != (
        True,
        False,
        True,
        True,
        False,
    ):
        raise PostgreSQLTestPreflightError("AURA_TEST_ROLE_PRIVILEGES_INVALID")
    if any(forbidden_access.values()):
        raise PostgreSQLTestPreflightError("AURA_TEST_CROSS_DATABASE_ACCESS")
    print("least-privilege role checks: passed")
    print("staging/public database access: rejected")
    print("AURA_POSTGRESQL_PREFLIGHT_OK")


def main() -> int:
    try:
        run_preflight()
    except PostgreSQLTestPreflightError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("AURA_TEST_PREFLIGHT_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
