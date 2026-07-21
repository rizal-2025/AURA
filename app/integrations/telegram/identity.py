"""HMAC-only Telegram identity and conversation-reference derivation."""

import hashlib
import hmac
import re


class InvalidTelegramIdentifier(ValueError):
    """Raised before an unsafe Telegram identifier can be persisted or used."""


def normalize_telegram_numeric_id(value) -> str:
    if isinstance(value, bool):
        raise InvalidTelegramIdentifier("Telegram identifier is invalid.")
    normalized = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]*", normalized):
        raise InvalidTelegramIdentifier("Telegram identifier is invalid.")
    return normalized


def _hmac_hex(identity_secret: str, value: str) -> str:
    if not isinstance(identity_secret, str) or not identity_secret:
        raise ValueError("Telegram identity secret is unavailable.")
    return hmac.new(
        identity_secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def derive_telegram_user_key(identity_secret: str, telegram_user_id) -> str:
    user_id = normalize_telegram_numeric_id(telegram_user_id)
    return _hmac_hex(identity_secret, f"aura:telegram:identity:v1:{user_id}")


def derive_telegram_session_reference(
    identity_secret: str,
    telegram_user_id,
    telegram_chat_id,
) -> str:
    user_id = normalize_telegram_numeric_id(telegram_user_id)
    chat_id = normalize_telegram_numeric_id(telegram_chat_id)
    return _hmac_hex(
        identity_secret,
        f"aura:telegram:private-session:v1:{user_id}:{chat_id}",
    )
