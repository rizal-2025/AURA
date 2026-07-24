from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import get_database_settings
from app.db.base import Base
import app.db.models.reservation
import app.db.models.customer
import app.db.models.support_ticket
import app.db.models.telegram_identity
import app.db.models.support_ticket_notification

database_settings = get_database_settings()

engine = create_engine(
    database_settings.DATABASE_URL,
    echo=database_settings.SQL_ECHO,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
