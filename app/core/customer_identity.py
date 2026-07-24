"""Immutable authenticated identity passed to application services."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AuthenticatedCustomer:
    id: UUID
    token_version: int
    is_active: bool
