from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.db.base import Base
import app.db.models.reservation
import app.db.models.customer
import app.db.models.support_ticket
import app.db.models.telegram_identity
import app.db.models.support_ticket_notification

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
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
