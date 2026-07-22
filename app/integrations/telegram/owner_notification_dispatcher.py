"""Persistent outbox dispatcher owned exclusively by the Telegram runner."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from app.core.logger import logger
from app.db.models.support_ticket import SupportTicket
from app.integrations.telegram.owner_notification_renderer import render_owner_notification
from app.services.handoff.notification_outbox_service import NotificationOutboxService


@dataclass(frozen=True)
class TelegramFailure:
    code: str
    retryable: bool
    retry_after_seconds: int | None = None


def classify_telegram_failure(error: Exception) -> TelegramFailure:
    name = type(error).__name__.lower()
    if "retryafter" in name:
        raw = getattr(error, "retry_after", None)
        if isinstance(raw, timedelta):
            raw = int(raw.total_seconds())
        delay = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        return TelegramFailure("rate_limited", True, min(max(delay or 1, 1), 3600))
    if "timedout" in name or isinstance(error, TimeoutError):
        return TelegramFailure("timeout", True)
    if "network" in name:
        return TelegramFailure("network_error", True)
    if "invalidtoken" in name or "unauthorized" in name:
        return TelegramFailure("invalid_token", False)
    if "forbidden" in name:
        return TelegramFailure("forbidden", False)
    if "badrequest" in name:
        # The text is inspected only for classification and is never stored or logged.
        normalized = str(error).lower()
        if "chat not found" in normalized:
            return TelegramFailure("chat_not_found", False)
        return TelegramFailure("bad_request", False)
    return TelegramFailure("unknown", False)


class OwnerNotificationDispatcher:
    def __init__(self, *, bot, session_factory, owner_chat_id: int, config, outbox_service=None):
        self.bot = bot
        self.session_factory = session_factory
        self.owner_chat_id = owner_chat_id
        self.config = config
        self.outbox_service = outbox_service or NotificationOutboxService()
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "OWNER NOTIFICATION: operation=poll status=failed code=database_error",
                )
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.config.owner_notification_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    async def process_once(self) -> bool:
        db = self.session_factory()
        try:
            notification = self.outbox_service.claim_due(
                db,
                lease_seconds=self.config.owner_notification_lease_seconds,
            )
            if notification is None:
                return False
            notification_id = notification.id
            ticket = db.get(SupportTicket, notification.support_ticket_id)
            chunks = render_owner_notification(ticket) if ticket is not None else None
        finally:
            db.close()

        if not chunks:
            self._mark_failure(notification_id, TelegramFailure("unknown", False))
            return True

        try:
            last_message_id = None
            for chunk in chunks:
                sent = await self.bot.send_message(chat_id=self.owner_chat_id, text=chunk)
                candidate = getattr(sent, "message_id", None)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    last_message_id = candidate
            update_db = self.session_factory()
            try:
                self.outbox_service.mark_sent(
                    update_db,
                    notification_id=notification_id,
                    telegram_message_id=last_message_id,
                )
            finally:
                update_db.close()
            logger.info(
                "OWNER NOTIFICATION: operation=send notification_id=%s status=sent",
                notification_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = classify_telegram_failure(error)
            self._mark_failure(notification_id, failure)
            logger.warning(
                "OWNER NOTIFICATION: operation=send notification_id=%s status=failed code=%s",
                notification_id,
                failure.code,
            )
        return True

    def _mark_failure(self, notification_id: int, failure: TelegramFailure) -> None:
        db = self.session_factory()
        try:
            self.outbox_service.mark_failed_attempt(
                db,
                notification_id=notification_id,
                error_code=failure.code,
                retryable=failure.retryable,
                max_attempts=self.config.owner_notification_max_attempts,
                retry_base_seconds=self.config.owner_notification_retry_base_seconds,
                retry_after_seconds=failure.retry_after_seconds,
            )
        finally:
            db.close()
