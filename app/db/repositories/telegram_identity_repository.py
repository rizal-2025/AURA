from datetime import datetime, timezone

from sqlalchemy import select

from app.core.ownership import require_owner_customer_id
from app.db.models.telegram_identity import TelegramIdentity


class TelegramIdentityRepository:
    def get_by_user_key(self, db, telegram_user_key: str):
        return db.execute(
            select(TelegramIdentity).where(
                TelegramIdentity.telegram_user_key == telegram_user_key,
            )
        ).scalar_one_or_none()

    def add(self, db, *, telegram_user_key: str, customer_id):
        require_owner_customer_id(customer_id)
        now = datetime.now(timezone.utc)
        identity = TelegramIdentity(
            telegram_user_key=telegram_user_key,
            customer_id=customer_id,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(identity)
        return identity
