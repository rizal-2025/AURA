from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from app.core.config import get_database_settings
from app.core.logger import logger
from app.core.transaction_errors import TransactionSessionUnusableError
from app.core.unit_of_work import mark_session_unusable
from app.db.base import Base
import app.db.models.reservation
import app.db.models.customer
import app.db.models.support_ticket
import app.db.models.telegram_identity
import app.db.models.support_ticket_notification
import app.db.models.conversation_workflow_state
import app.db.models.demo_persistence

database_settings = get_database_settings()


def deployment_engine_options(database_url: str) -> dict:
    """Use a small bounded pool for managed PostgreSQL demo services."""
    try:
        backend = make_url(database_url).get_backend_name().casefold()
    except (AttributeError, TypeError, ValueError):
        return {}
    if backend not in {"postgres", "postgresql"}:
        return {}
    return {
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": 5,
        "pool_recycle": 300,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
    }

engine = create_engine(
    database_settings.DATABASE_URL,
    echo=database_settings.SQL_ECHO,
    **deployment_engine_options(database_settings.DATABASE_URL),
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def get_db():
    db = SessionLocal()
    application_error = None
    cleanup_error = None
    try:
        yield db
    except BaseException as error:
        application_error = error
    finally:
        try:
            if db.in_transaction():
                db.rollback()
        except BaseException as error:
            mark_session_unusable(db)
            cleanup_error = error
        finally:
            try:
                db.close()
            except BaseException as error:
                mark_session_unusable(db)
                if cleanup_error is None:
                    cleanup_error = error

    if application_error is not None:
        if cleanup_error is not None:
            logger.error(
                "DATABASE SESSION: operation=cleanup status=failed "
                "code=cleanup_failure_after_application_error",
            )
        raise application_error.with_traceback(application_error.__traceback__) from None

    if cleanup_error is not None:
        logger.error(
            "DATABASE SESSION: operation=cleanup status=failed "
            "code=session_unusable",
        )
        raise TransactionSessionUnusableError() from None
