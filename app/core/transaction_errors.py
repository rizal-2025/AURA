"""Stable transaction errors safe for public adapters and operational logs."""


class _SafeTransactionError(RuntimeError):
    code = "PERSISTENCE_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class PersistenceOperationError(_SafeTransactionError):
    """A business mutation failed before a commit was attempted."""

    code = "PERSISTENCE_OPERATION_FAILED"


class PersistenceOutcomeUnknownError(_SafeTransactionError):
    """The database outcome could not be proven at the commit boundary."""

    code = "PERSISTENCE_OUTCOME_UNKNOWN"


class TransactionSessionUnusableError(_SafeTransactionError):
    """The current Session must be discarded and never reused."""

    code = "PERSISTENCE_SESSION_UNAVAILABLE"
