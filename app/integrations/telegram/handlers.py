"""Private-chat Telegram handlers that delegate to the shared chat boundary."""

from app.core.config import settings
from app.core.logger import logger
from app.db.database import SessionLocal
from app.integrations.telegram.identity import derive_telegram_session_reference
from app.integrations.telegram.identity_service import (
    TelegramIdentityService,
    TelegramIdentityUnavailableError,
)
from app.integrations.telegram.message_utils import split_telegram_reply
from app.services.authenticated_chat_service import authenticated_chat_service


PRIVATE_CHAT_ONLY_REPLY = "Bot ini saat ini hanya mendukung chat pribadi."
IDENTITY_UNAVAILABLE_REPLY = "Identitas Telegram tidak tersedia. Silakan coba lagi nanti."
SERVICE_UNAVAILABLE_REPLY = "Maaf, layanan AURA sedang tidak tersedia. Silakan coba lagi."
WELCOME_REPLY = (
    "Halo, saya AURA. Saya dapat membantu reservasi, melihat, mengubah, atau "
    "membatalkan reservasi Anda."
)
HELP_REPLY = (
    "Contoh: buatkan reservasi, lihat reservasi saya, ubah reservasi saya, "
    "atau batalkan reservasi saya."
)


class TelegramCustomerHandlers:
    """Framework-neutral handler implementation; PTB objects are duck-typed."""

    def __init__(
        self,
        *,
        identity_secret: str | None = None,
        session_factory=None,
        identity_service=None,
        chat_service=None,
    ):
        self.identity_secret = identity_secret or settings.TELEGRAM_IDENTITY_SECRET
        self.session_factory = session_factory or SessionLocal
        self.identity_service = identity_service or TelegramIdentityService()
        self.chat_service = chat_service or authenticated_chat_service

    async def _validated_private_context(self, update):
        if update is None:
            logger.info("TELEGRAM UPDATE: outcome=rejected category=missing_update")
            return None
        message = getattr(update, "effective_message", None)
        if message is None:
            logger.info("TELEGRAM UPDATE: outcome=rejected category=missing_message")
            return None
        chat = getattr(update, "effective_chat", None)
        if chat is None or getattr(chat, "id", None) is None:
            logger.info("TELEGRAM UPDATE: outcome=rejected category=missing_chat")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_REPLY)
            return None
        if getattr(chat, "type", None) != "private":
            await self._safe_reply(message, PRIVATE_CHAT_ONLY_REPLY)
            return None
        user = getattr(update, "effective_user", None)
        if user is None or getattr(user, "id", None) is None:
            logger.info("TELEGRAM UPDATE: outcome=rejected category=missing_user")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_REPLY)
            return None
        return user, chat, message

    @staticmethod
    async def _safe_reply(message, text: str) -> bool:
        if message is None or not callable(getattr(message, "reply_text", None)):
            logger.warning("TELEGRAM SEND: outcome=skipped category=missing_message")
            return False
        for chunk in split_telegram_reply(text):
            try:
                # Deliberately no parse mode: AURA responses remain plain text.
                await message.reply_text(chunk)
            except Exception:
                logger.warning("TELEGRAM SEND: outcome=failed category=send_error")
                return False
        return True

    async def _resolve_customer(self, user, chat):
        db = self.session_factory()
        try:
            customer = self.identity_service.resolve_or_create(
                db,
                telegram_user_id=user.id,
                identity_secret=self.identity_secret,
            )
            session_reference = derive_telegram_session_reference(
                self.identity_secret,
                user.id,
                chat.id,
            )
            return db, customer, session_reference
        except Exception:
            db.rollback()
            db.close()
            raise

    async def _customer_message(self, update, message: str) -> None:
        context = await self._validated_private_context(update)
        if context is None:
            return
        user, chat, telegram_message = context
        try:
            db, customer, session_reference = await self._resolve_customer(user, chat)
        except TelegramIdentityUnavailableError:
            logger.info("TELEGRAM UPDATE: outcome=identity_unavailable")
            await self._safe_reply(telegram_message, IDENTITY_UNAVAILABLE_REPLY)
            return
        except Exception:
            logger.info("TELEGRAM UPDATE: outcome=identity_error")
            await self._safe_reply(telegram_message, SERVICE_UNAVAILABLE_REPLY)
            return

        try:
            response = await self.chat_service.process(
                db=db,
                customer=customer,
                session_reference=session_reference,
                message=message,
            )
            if await self._safe_reply(telegram_message, response):
                logger.info("TELEGRAM UPDATE: outcome=handled")
            else:
                db.rollback()
                await self._safe_reply(telegram_message, SERVICE_UNAVAILABLE_REPLY)
        except Exception:
            db.rollback()
            logger.info("TELEGRAM UPDATE: outcome=service_error")
            await self._safe_reply(telegram_message, SERVICE_UNAVAILABLE_REPLY)
        finally:
            db.close()

    async def _ensure_identity(self, update) -> bool:
        """Create/validate the trusted mapping without entering an AURA workflow."""
        context = await self._validated_private_context(update)
        if context is None:
            return False
        user, chat, telegram_message = context
        try:
            db, _customer, _session_reference = await self._resolve_customer(user, chat)
        except TelegramIdentityUnavailableError:
            logger.info("TELEGRAM UPDATE: outcome=identity_unavailable")
            await self._safe_reply(telegram_message, IDENTITY_UNAVAILABLE_REPLY)
            return False
        except Exception:
            logger.info("TELEGRAM UPDATE: outcome=identity_error")
            await self._safe_reply(telegram_message, SERVICE_UNAVAILABLE_REPLY)
            return False
        db.close()
        return True

    async def start(self, update, context) -> None:
        if await self._ensure_identity(update):
            await self._safe_reply(update.effective_message, WELCOME_REPLY)

    async def help(self, update, context) -> None:
        if await self._ensure_identity(update):
            await self._safe_reply(update.effective_message, HELP_REPLY)

    async def status(self, update, context) -> None:
        private_context = await self._validated_private_context(update)
        if private_context is None:
            return
        user, chat, message = private_context
        try:
            db, customer, session_reference = await self._resolve_customer(user, chat)
        except TelegramIdentityUnavailableError:
            logger.info("TELEGRAM UPDATE: outcome=identity_unavailable")
            await self._safe_reply(message, IDENTITY_UNAVAILABLE_REPLY)
            return
        except Exception:
            logger.info("TELEGRAM UPDATE: outcome=identity_error")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_REPLY)
            return
        try:
            response = self.chat_service.ticket_status(
                db=db,
                customer=customer,
                session_reference=session_reference,
            )
            if not await self._safe_reply(message, response):
                db.rollback()
        except Exception:
            db.rollback()
            logger.info("TELEGRAM UPDATE: outcome=status_error")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_REPLY)
        finally:
            db.close()

    async def text_message(self, update, context) -> None:
        message = getattr(update, "effective_message", None)
        text = getattr(message, "text", None)
        if not isinstance(text, str) or not text.strip():
            await self.non_text_message(update, context)
            return
        if text.lstrip().startswith("/"):
            if await self._validated_private_context(update) is not None:
                await self._safe_reply(message, HELP_REPLY)
            return
        await self._customer_message(update, text)

    async def non_text_message(self, update, context) -> None:
        private_context = await self._validated_private_context(update)
        if private_context is None:
            return
        _user, _chat, message = private_context
        await self._safe_reply(message, "Saat ini saya hanya dapat memproses pesan teks.")
