"""Safe lifecycle helper for PostgreSQL schemas created by integration tests."""

from __future__ import annotations

import re

from sqlalchemy import text


class DisposableSchemaCleanupError(RuntimeError):
    """Stable error raised only after every cleanup action has been attempted."""


class DisposableSchemaResources:
    """Track and clean internally generated disposable PostgreSQL resources."""

    def __init__(
        self,
        *,
        admin_engine,
        schema: str,
        allowed_prefixes: tuple[str, ...],
        dispose_admin: bool,
    ):
        self.admin_engine = admin_engine
        self.allowed_prefixes = allowed_prefixes
        self.dispose_admin = dispose_admin
        self._schemas = [schema]
        self._engines = []

    def track_engine(self, engine) -> None:
        self._engines.append(engine)

    def track_schema(self, schema: str) -> None:
        self._schemas.append(schema)

    def _quoted_schema(self, schema: str) -> str:
        is_generated = any(
            re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{10,32}}", schema)
            for prefix in self.allowed_prefixes
        )
        if not is_generated or schema == "public":
            raise DisposableSchemaCleanupError(
                "Refusing to clean a non-disposable schema."
            )
        return f'"{schema}"'

    def cleanup(self) -> None:
        failures: list[BaseException] = []

        for engine in reversed(self._engines):
            try:
                engine.dispose()
            except BaseException as error:
                failures.append(error)

        for schema in reversed(self._schemas):
            try:
                quoted_schema = self._quoted_schema(schema)
                with self.admin_engine.begin() as connection:
                    connection.execute(
                        text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
                    )
            except BaseException as error:
                failures.append(error)

        if self.dispose_admin:
            try:
                self.admin_engine.dispose()
            except BaseException as error:
                failures.append(error)

        if failures:
            raise DisposableSchemaCleanupError(
                "Disposable PostgreSQL cleanup failed."
            ) from None
