"""Detached reservation values read from persistence.

These DTOs intentionally do not reuse current Create or Update validators.
Rows that are valid under the database schema remain readable even when newer
input policy is stricter.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PersistedReservationDTO:
    id: int
    name: str
    people: int
    date: str
    time: str
    status: str
    reference: str | None = None
