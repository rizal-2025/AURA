from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.db.models.support_ticket_notification import (
    TELEGRAM_OWNER_CHANNEL,
    VALID_NOTIFICATION_ERROR_CODES,
    SupportTicketNotification,
)


class SupportTicketNotificationRepository:
    def add_pending(self, db, *, support_ticket_id: int, now: datetime | None = None):
        if isinstance(support_ticket_id, bool) or not isinstance(support_ticket_id, int) or support_ticket_id < 1:
            raise ValueError("A persisted support ticket is required.")
        timestamp = now or datetime.now(timezone.utc)
        notification = SupportTicketNotification(
            support_ticket_id=support_ticket_id,
            channel=TELEGRAM_OWNER_CHANNEL,
            status="pending",
            attempt_count=0,
            next_attempt_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        db.add(notification)
        db.flush()
        return notification

    def get_for_ticket(self, db, *, support_ticket_id: int):
        return db.execute(
            select(SupportTicketNotification).where(
                SupportTicketNotification.support_ticket_id == support_ticket_id,
                SupportTicketNotification.channel == TELEGRAM_OWNER_CHANNEL,
            )
        ).scalar_one_or_none()

    def claim_due(self, db, *, lease_seconds: int, now: datetime | None = None):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("Notification lease must be a positive integer.")
        timestamp = now or datetime.now(timezone.utc)
        statement = (
            select(SupportTicketNotification)
            .where(
                or_(
                    and_(
                        SupportTicketNotification.status == "pending",
                        SupportTicketNotification.next_attempt_at <= timestamp,
                    ),
                    and_(
                        SupportTicketNotification.status == "sending",
                        SupportTicketNotification.lease_expires_at.is_not(None),
                        SupportTicketNotification.lease_expires_at <= timestamp,
                    ),
                )
            )
            .order_by(
                SupportTicketNotification.next_attempt_at.asc(),
                SupportTicketNotification.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        try:
            notification = db.execute(statement).scalar_one_or_none()
            if notification is None:
                db.rollback()
                return None
            notification.status = "sending"
            notification.lease_expires_at = timestamp + timedelta(seconds=lease_seconds)
            notification.updated_at = timestamp
            db.commit()
            db.refresh(notification)
            return notification
        except Exception:
            db.rollback()
            raise

    def mark_sent(self, db, *, notification_id: int, telegram_message_id=None, now=None):
        timestamp = now or datetime.now(timezone.utc)
        try:
            notification = db.execute(
                select(SupportTicketNotification)
                .where(
                    SupportTicketNotification.id == notification_id,
                    SupportTicketNotification.status == "sending",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if notification is None:
                db.rollback()
                return None
            if telegram_message_id is not None and (
                isinstance(telegram_message_id, bool) or not isinstance(telegram_message_id, int)
            ):
                telegram_message_id = None
            notification.status = "sent"
            notification.sent_at = timestamp
            notification.telegram_message_id = telegram_message_id
            notification.last_error_code = None
            notification.lease_expires_at = None
            notification.updated_at = timestamp
            db.commit()
            db.refresh(notification)
            return notification
        except Exception:
            db.rollback()
            raise

    def mark_failed_attempt(
        self,
        db,
        *,
        notification_id: int,
        error_code: str,
        retryable: bool,
        max_attempts: int,
        retry_base_seconds: int,
        retry_after_seconds: int | None = None,
        now: datetime | None = None,
    ):
        if error_code not in VALID_NOTIFICATION_ERROR_CODES:
            error_code = "unknown"
        timestamp = now or datetime.now(timezone.utc)
        try:
            notification = db.execute(
                select(SupportTicketNotification)
                .where(
                    SupportTicketNotification.id == notification_id,
                    SupportTicketNotification.status == "sending",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if notification is None:
                db.rollback()
                return None
            notification.attempt_count += 1
            notification.last_error_code = error_code
            notification.lease_expires_at = None
            notification.updated_at = timestamp
            if retryable and notification.attempt_count < max_attempts:
                exponential = retry_base_seconds * (2 ** (notification.attempt_count - 1))
                delay = min(exponential, 3600)
                if retry_after_seconds is not None:
                    delay = min(max(delay, retry_after_seconds), 3600)
                notification.status = "pending"
                notification.next_attempt_at = timestamp + timedelta(seconds=delay)
            else:
                notification.status = "failed"
            db.commit()
            db.refresh(notification)
            return notification
        except Exception:
            db.rollback()
            raise

