"""Public-safe identifiers for persisted reservations."""

from __future__ import annotations

import re
import secrets

from app.core.transaction_errors import PersistenceOperationError


PUBLIC_REFERENCE_PREFIX = "RSV_"
PUBLIC_REFERENCE_TOKEN_LENGTH = 32
PUBLIC_REFERENCE_LENGTH = 36
PUBLIC_REFERENCE_ENTROPY_BYTES = 16
PUBLIC_REFERENCE_MAX_ATTEMPTS = 5
PUBLIC_REFERENCE_UNIQUE_CONSTRAINT = "uq_reservations_public_reference"

_PUBLIC_REFERENCE_PATTERN = re.compile(
    r"^RSV_[0-9a-f]{32}$",
    flags=re.IGNORECASE,
)


class InvalidPublicReservationReferenceError(ValueError):
    """A stable error that never reflects malformed input."""

    code = "INVALID_RESERVATION_REFERENCE"

    def __init__(self) -> None:
        super().__init__(self.code)


class PublicReservationReferenceCollisionError(PersistenceOperationError):
    """All bounded attempts to allocate a unique reference were exhausted."""

    code = "RESERVATION_REFERENCE_UNAVAILABLE"


class PublicReservationReferenceUnavailableError(PersistenceOperationError):
    """A persisted reservation cannot cross a public-safe boundary."""

    code = "RESERVATION_REFERENCE_UNAVAILABLE"


def generate_public_reference() -> str:
    """Generate one canonical reference with exactly 128 bits of entropy."""

    return PUBLIC_REFERENCE_PREFIX + secrets.token_hex(
        PUBLIC_REFERENCE_ENTROPY_BYTES
    )


def canonicalize_public_reference(value: str) -> str:
    """Validate the complete input and return its canonical stored form."""

    if not isinstance(value, str) or _PUBLIC_REFERENCE_PATTERN.fullmatch(value) is None:
        raise InvalidPublicReservationReferenceError()
    return PUBLIC_REFERENCE_PREFIX + value[len(PUBLIC_REFERENCE_PREFIX) :].lower()


def is_valid_public_reference(value: object) -> bool:
    """Return whether a value is a strict full-string public reference."""

    if not isinstance(value, str):
        return False
    return _PUBLIC_REFERENCE_PATTERN.fullmatch(value) is not None


def require_canonical_public_reference(value: object) -> str:
    """Return a stored canonical reference or fail with a stable safe error."""

    try:
        canonical = canonicalize_public_reference(value)
    except InvalidPublicReservationReferenceError:
        raise PublicReservationReferenceUnavailableError() from None
    if value != canonical:
        raise PublicReservationReferenceUnavailableError()
    return canonical
