"""Internal conversation-memory scoping for authenticated chat requests."""

from app.core.ownership import require_owner_customer_id


def build_authenticated_memory_key(owner_customer_id, session_id: str) -> str:
    """Return an internal-only key that scopes a client session to its owner.

    ``session_id`` remains a client-controlled conversation label. It must never
    be used by itself for authenticated chat memory because another customer can
    choose the same label.
    """
    owner_customer_id = require_owner_customer_id(owner_customer_id)
    return f"{owner_customer_id}:{session_id}"
