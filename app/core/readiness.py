"""Detail-free database readiness probe shared by local operators."""


def database_is_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db.database import engine

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
