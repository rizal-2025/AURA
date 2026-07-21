"""Resolve Telegram users to server-owned Customers without bearer tokens."""

from sqlalchemy.exc import IntegrityError

from app.core.ownership import require_owner_customer_id
from app.db.models.customer import Customer
from app.db.repositories.telegram_identity_repository import TelegramIdentityRepository
from app.integrations.telegram.identity import derive_telegram_user_key


class TelegramIdentityUnavailableError(RuntimeError):
    """Safe fail-closed identity error for inactive or missing trusted records."""


class TelegramIdentityService:
    def __init__(self, repository=None):
        self.repository = repository or TelegramIdentityRepository()

    def resolve_or_create(self, db, *, telegram_user_id, identity_secret: str) -> Customer:
        user_key = derive_telegram_user_key(identity_secret, telegram_user_id)
        identity = self.repository.get_by_user_key(db, user_key)
        if identity is not None:
            return self._active_customer_for_identity(db, identity)

        try:
            customer = Customer()
            db.add(customer)
            db.flush()
            require_owner_customer_id(customer.id)
            identity = self.repository.add(
                db,
                telegram_user_key=user_key,
                customer_id=customer.id,
            )
            db.flush()
            db.commit()
            db.refresh(customer)
            db.refresh(identity)
            return self._active_customer_for_identity(db, identity, customer=customer)
        except IntegrityError:
            # A competing first update won. The rollback removes the losing
            # Customer insert as well, preventing orphan customer records.
            db.rollback()
            identity = self.repository.get_by_user_key(db, user_key)
            if identity is None:
                raise TelegramIdentityUnavailableError("Telegram identity is unavailable.")
            return self._active_customer_for_identity(db, identity)
        except Exception:
            db.rollback()
            raise

    def _active_customer_for_identity(self, db, identity, customer=None) -> Customer:
        if identity is None or not identity.is_active:
            raise TelegramIdentityUnavailableError("Telegram identity is unavailable.")
        if customer is None:
            customer = db.get(Customer, identity.customer_id)
        if customer is None or not customer.is_active:
            raise TelegramIdentityUnavailableError("Telegram identity is unavailable.")
        require_owner_customer_id(customer.id)
        return customer
