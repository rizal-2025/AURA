"""Thin Telegram callbacks for owner-only support-ticket management."""

from app.core.logger import logger
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.message_utils import split_telegram_reply
from app.integrations.telegram.owner_authorization import authorize_owner_update
from app.integrations.telegram.owner_command_renderer import (
    GENERIC_COMMAND_REPLY,
    render_owner_command,
)
from app.services.handoff.owner_ticket_service import OwnerTicketResult, OwnerTicketService


class TelegramOwnerCommandHandlers:
    def __init__(self, *, owner_chat_id: int, session_factory=None, ticket_service=None):
        self.owner_chat_id = owner_chat_id
        self.session_factory = session_factory or self._default_session_factory
        self.ticket_service = ticket_service or OwnerTicketService()

    @staticmethod
    def _default_session_factory():
        from app.db.database import SessionLocal

        return SessionLocal()

    @staticmethod
    async def _send_chunks(message, chunks: list[str]) -> bool:
        """Use the proven Phase D plain-text sender for every renderer chunk."""
        if not isinstance(chunks, (list, tuple)):
            return False
        for chunk in chunks:
            # Renderer output is a sequence of already-safe text chunks. Reject
            # unexpected values instead of coercing DTOs, lists, or coroutines
            # into a Telegram message body.
            if not isinstance(chunk, str):
                return False
            if not await TelegramCustomerHandlers._safe_reply(message, chunk):
                return False
        return True

    async def _reply(self, message, text: str) -> bool:
        return await self._send_chunks(message, split_telegram_reply(text))

    @staticmethod
    def _safe_log_result(command: str, result_code: str) -> str:
        if result_code == "success":
            return "updated" if command in {"take", "resolve"} else "authorized"
        if result_code == "empty":
            return "authorized"
        if result_code == "closed":
            return "not_available"
        return result_code if result_code in {
            "invalid_argument",
            "not_available",
            "already_in_progress",
            "already_resolved",
            "database_error",
        } else "internal_error"

    async def _handle(self, update, context, *, command: str) -> None:
        authorized = authorize_owner_update(update, self.owner_chat_id)
        if authorized is None:
            logger.info("OWNER COMMAND: command=%s result=denied", command)
            message = getattr(update, "effective_message", None) if update is not None else None
            if not await self._reply(message, GENERIC_COMMAND_REPLY) and message is not None:
                logger.info("OWNER COMMAND: command=%s result=send_error", command)
            return

        arguments = getattr(context, "args", None)
        arguments = list(arguments) if isinstance(arguments, (list, tuple)) else []
        valid_arguments = not arguments if command == "tickets" else (
            len(arguments) == 1
            and OwnerTicketService.valid_ticket_number(arguments[0])
        )
        if not valid_arguments:
            result = OwnerTicketResult("invalid_argument")
            sent = await self._send_chunks(
                authorized.message, render_owner_command(command, result)
            )
            logger.info(
                "OWNER COMMAND: command=%s result=%s",
                command,
                "invalid_argument" if sent else "send_error",
            )
            return

        db = None
        try:
            db = self.session_factory()
            if command == "tickets":
                result = self.ticket_service.list_active_tickets(db, limit=10)
            elif command == "ticket":
                result = self.ticket_service.get_ticket(db, arguments[0])
            elif command == "take":
                result = self.ticket_service.take_ticket(db, arguments[0])
            else:
                result = self.ticket_service.resolve_ticket(db, arguments[0])
        except Exception:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            result = OwnerTicketResult("database_error")
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

        sent = await self._send_chunks(
            authorized.message, render_owner_command(command, result)
        )
        logger.info(
            "OWNER COMMAND: command=%s result=%s",
            command,
            self._safe_log_result(command, result.code) if sent else "send_error",
        )

    async def tickets(self, update, context) -> None:
        await self._handle(update, context, command="tickets")

    async def ticket(self, update, context) -> None:
        await self._handle(update, context, command="ticket")

    async def take(self, update, context) -> None:
        await self._handle(update, context, command="take")

    async def resolve(self, update, context) -> None:
        await self._handle(update, context, command="resolve")


async def unknown_command(update, context) -> None:
    """Reject unsupported commands without identity, database, or AI access."""
    message = getattr(update, "effective_message", None) if update is not None else None
    await TelegramOwnerCommandHandlers._send_chunks(
        message, split_telegram_reply(GENERIC_COMMAND_REPLY)
    )
