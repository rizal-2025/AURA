"""Dependency-light safe errors for the internal demo chat boundary."""


class _SafeDemoChatError(RuntimeError):
    code = "DEMO_CHAT_ERROR"

    def __init__(self):
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class DemoChatRequestConflictError(_SafeDemoChatError):
    code = "REQUEST_CONFLICT"


class DemoChatProviderError(_SafeDemoChatError):
    code = "PROVIDER_ERROR"


class DemoChatProviderTimeoutError(_SafeDemoChatError):
    code = "PROVIDER_TIMEOUT"


class DemoChatServiceUnavailableError(_SafeDemoChatError):
    code = "SERVICE_UNAVAILABLE"
