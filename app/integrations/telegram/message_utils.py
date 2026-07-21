"""Safe plain-text helpers for Telegram customer replies."""


TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_telegram_reply(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a plain-text response without exceeding Telegram's message limit."""
    if not isinstance(text, str):
        text = str(text)
    if limit <= 0:
        raise ValueError("Telegram message limit must be positive.")
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n\n", 0, limit + 1)
        if boundary <= 0:
            boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary <= 0:
            boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
        if remaining.startswith("\n\n"):
            remaining = remaining[2:]
        elif remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]
    chunks.append(remaining)
    return chunks
