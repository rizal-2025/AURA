"""Safely add the nullable reservations.customer_id ownership column."""

from pathlib import Path
import sys

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import engine


TABLE_NAME = "reservations"
COLUMN_NAME = "customer_id"


def migrate() -> bool:
    """Add customer_id if needed, without creating, dropping, or deleting data."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(TABLE_NAME):
            raise RuntimeError(
                "Tabel 'reservations' tidak ditemukan. "
                "Migrasi tidak akan membuat atau membuat ulang tabel tersebut."
            )

        column_names = {
            column["name"]
            for column in inspector.get_columns(TABLE_NAME)
        }
        if COLUMN_NAME in column_names:
            print("Kolom reservations.customer_id sudah tersedia.")
            return False

        connection.execute(
            text(
                "ALTER TABLE reservations "
                "ADD COLUMN IF NOT EXISTS customer_id VARCHAR(255) NULL"
            )
        )
        print("Kolom reservations.customer_id berhasil ditambahkan.")
        return True


if __name__ == "__main__":
    migrate()
