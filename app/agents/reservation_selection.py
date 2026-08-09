"""Deterministic, public-safe reservation selection presentation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.brain.reservation_entity_extractor import (
    PublicReferenceParseStatus,
    parse_public_reservation_reference,
)


INDONESIAN_MONTHS = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
)


@dataclass(frozen=True)
class ReservationSelection:
    status: str
    reference: str | None = None


def parse_reservation_selection(
    value: str,
    candidate_references: tuple[str, ...],
) -> ReservationSelection:
    """Resolve a displayed number or a backwards-compatible public reference."""

    normalized = value.strip() if isinstance(value, str) else ""
    if re.fullmatch(r"[+-]?\d+", normalized):
        choice = int(normalized)
        if 1 <= choice <= len(candidate_references):
            return ReservationSelection(
                "valid",
                candidate_references[choice - 1],
            )
        return ReservationSelection("out_of_range")

    parsed_reference = parse_public_reservation_reference(normalized)
    if parsed_reference.status is PublicReferenceParseStatus.VALID:
        return ReservationSelection("valid", parsed_reference.reference)
    if parsed_reference.status is PublicReferenceParseStatus.AMBIGUOUS:
        return ReservationSelection("ambiguous")
    return ReservationSelection("invalid")


def format_reservation_summary(reservation) -> str:
    return (
        f"Nama: {reservation.name}\n"
        f"Tanggal: {_format_date(reservation.date)}\n"
        f"Jam: {_format_time(reservation.time)}\n"
        f"Jumlah: {reservation.people} orang"
    )


def format_numbered_reservations(reservations) -> str:
    return "\n".join(
        f"{index}. {_format_date(reservation.date)} · "
        f"{_format_time(reservation.time)} · {reservation.people} orang"
        for index, reservation in enumerate(reservations, start=1)
    )


def _format_date(value: object) -> str:
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return text
    return f"{parsed.day} {INDONESIAN_MONTHS[parsed.month]} {parsed.year}"


def _format_time(value: object) -> str:
    text = str(value)
    return text[:5].replace(":", ".") if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", text) else text
