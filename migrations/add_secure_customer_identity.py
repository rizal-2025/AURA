"""Safely add the V1.5 secure customer-identity foundation."""

from pathlib import Path
import sys

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


RESERVATIONS_TABLE = "reservations"
CUSTOMERS_TABLE = "customers"
OWNER_COLUMN = "owner_customer_id"
FOREIGN_KEY_NAME = "fk_reservations_owner_customer_id"
INDEX_NAME = "ix_reservations_owner_customer_id_id"


def migrate() -> bool:
    """Create only missing V1.5 structures; never alter reservation data."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(RESERVATIONS_TABLE):
            raise RuntimeError(
                "Tabel 'reservations' tidak ditemukan. "
                "Migrasi tidak akan membuat atau membuat ulang tabel tersebut."
            )

        changed = False
        if not inspector.has_table(CUSTOMERS_TABLE):
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS customers ("
                    "id UUID PRIMARY KEY, "
                    "token_version INTEGER NOT NULL DEFAULT 1, "
                    "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
                    "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP"
                    ")"
                )
            )
            changed = True

        column_names = {
            column["name"] for column in inspector.get_columns(RESERVATIONS_TABLE)
        }
        if OWNER_COLUMN not in column_names:
            connection.execute(
                text(
                    "ALTER TABLE reservations "
                    "ADD COLUMN IF NOT EXISTS owner_customer_id UUID NULL"
                )
            )
            changed = True

        foreign_keys = inspector.get_foreign_keys(RESERVATIONS_TABLE)
        has_owner_foreign_key = any(
            foreign_key.get("constrained_columns") == [OWNER_COLUMN]
            and foreign_key.get("referred_table") == CUSTOMERS_TABLE
            for foreign_key in foreign_keys
        )
        if not has_owner_foreign_key:
            connection.execute(
                text(
                    "ALTER TABLE reservations "
                    "ADD CONSTRAINT fk_reservations_owner_customer_id "
                    "FOREIGN KEY (owner_customer_id) REFERENCES customers(id)"
                )
            )
            changed = True

        index_names = {
            index["name"] for index in inspector.get_indexes(RESERVATIONS_TABLE)
        }
        if INDEX_NAME not in index_names:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_reservations_owner_customer_id_id "
                    "ON reservations (owner_customer_id, id DESC)"
                )
            )
            changed = True

        if changed:
            print("Fondasi secure customer identity V1.5 berhasil ditambahkan.")
        else:
            print("Fondasi secure customer identity V1.5 sudah tersedia.")
        return changed


if __name__ == "__main__":
    migrate()
