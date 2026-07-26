"""Stable conversation-memory errors safe for public adapters."""


class ConversationMemoryError(RuntimeError):
    """Base class whose public rendering never includes conversation data."""

    code = "CONVERSATION_MEMORY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class ConversationMemoryValidationError(ConversationMemoryError):
    """Conversation state cannot be represented by the bounded safe model."""

    code = "CONVERSATION_MEMORY_UNAVAILABLE"


class PostCommitMemoryPublicationError(ConversationMemoryError):
    """The database commit is confirmed but success state was not published."""

    code = "COMMITTED_OPERATION_STATE_UNAVAILABLE"


class ReservationMutationGuardError(ConversationMemoryError):
    """The process-local fail-closed reservation guard could not be installed."""

    code = "RESERVATION_MUTATION_GUARD_UNAVAILABLE"
