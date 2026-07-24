"""Resolve Telegram users to server-owned Customers without bearer tokens."""

from sqlalchemy.exc import IntegrityError

from app.core.customer_identity import AuthenticatedCustomer
from app.core.ownership import require_owner_customer_id
from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.repositories.telegram_identity_repository import TelegramIdentityRepository
from app.integrations.telegram.identity import derive_telegram_user_key


class TelegramIdentityUnavailableError(RuntimeError):
    """Safe fail-closed identity error for inactive or missing trusted records."""


class TelegramIdentityService:
    def __init__(self, repository=None):
        self.repository = repository or TelegramIdentityRepository()

    def resolve_or_create(
        self,
        db,
        *,
        telegram_user_id,
        identity_secret: str,
    ) -> AuthenticatedCustomer:
        user_key = derive_telegram_user_key(identity_secret, telegram_user_id)
        try:
            with UnitOfWork(db) as unit:
                identity = self.repository.get_by_user_key(db, user_key)
                if identity is None:
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
                    context = self._customer_context(
                        db,
                        identity,
                        customer=customer,
                    )
                else:
                    context = self._customer_context(db, identity)
                unit.commit()
            if context is None:
                raise TelegramIdentityUnavailableError(
                    "Telegram identity is unavailable."
                )
            return context
        except PersistenceOperationError as error:
            if not isinstance(error.__cause__, IntegrityError):
                raise

        # The losing Customer and identity rows were rolled back by UnitOfWork.
        # Resolve the committed winner in a fresh transaction.
        with UnitOfWork(db) as unit:
            identity = self.repository.get_by_user_key(db, user_key)
            context = self._customer_context(db, identity)
            unit.commit()
        if context is None:
            raise TelegramIdentityUnavailableError(
                "Telegram identity is unavailable."
            )
        return context

    @staticmethod
    def _customer_context(db, identity, customer=None) -> AuthenticatedCustomer | None:
        if identity is None or not identity.is_active:
            return None
        if customer is None:
            customer = db.get(Customer, identity.customer_id)
        if customer is None or not customer.is_active:
            return None
        require_owner_customer_id(customer.id)
        return AuthenticatedCustomer(
            id=customer.id,
            token_version=int(customer.token_version),
            is_active=bool(customer.is_active),
        )
