"""Pure, fail-closed authorization for runner-injected Telegram owner identity."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorizedOwnerContext:
    message: object


def authorize_owner_update(update, configured_owner_id) -> AuthorizedOwnerContext | None:
    if (
        isinstance(configured_owner_id, bool)
        or not isinstance(configured_owner_id, int)
        or configured_owner_id <= 0
        or update is None
    ):
        return None

    message = getattr(update, "effective_message", None)
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if message is None or chat is None or user is None:
        return None

    chat_id = getattr(chat, "id", None)
    user_id = getattr(user, "id", None)
    if (
        getattr(chat, "type", None) != "private"
        or isinstance(chat_id, bool)
        or isinstance(user_id, bool)
        or chat_id != configured_owner_id
        or user_id != configured_owner_id
        or chat_id != user_id
    ):
        return None

    # Telegram may represent anonymous/channel-origin messages via sender_chat or
    # forwarding metadata. Owner commands never accept those contexts.
    for attribute in (
        "sender_chat",
        "forward_origin",
        "forward_from",
        "forward_from_chat",
        "forward_sender_name",
    ):
        if getattr(message, attribute, None) is not None:
            return None
    if bool(getattr(message, "is_automatic_forward", False)):
        return None

    return AuthorizedOwnerContext(message=message)
