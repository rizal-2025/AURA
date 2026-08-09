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
NEXT_PAGE_COMMAND = "berikutnya"
FIRST_PAGE_COMMAND = "awal"


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
    command = " ".join(normalized.casefold().split())
    if command == NEXT_PAGE_COMMAND:
        return ReservationSelection("next_page")
    if command == FIRST_PAGE_COMMAND:
        return ReservationSelection("first_page")
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


def format_paginated_selection(
    reservations,
    *,
    has_more: bool,
    is_later_page: bool,
) -> str:
    count = len(reservations)
    if is_later_page:
        heading = "Reservasi lainnya:"
    elif has_more:
        heading = "Saya menemukan beberapa reservasi:"
    else:
        heading = f"Saya menemukan {count} reservasi:"

    if not has_more and not is_later_page:
        guidance = f"Pilih reservasi: 1 sampai {count}."
    else:
        guidance_lines = [f"Ketik 1 sampai {count} untuk memilih."]
        if has_more:
            guidance_lines.append(
                f'Ketik "{NEXT_PAGE_COMMAND}" untuk melihat reservasi lainnya.'
            )
        if is_later_page:
            guidance_lines.append(
                f'Ketik "{FIRST_PAGE_COMMAND}" untuk kembali ke daftar awal.'
            )
        guidance = "\n".join(guidance_lines)

    return (
        f"{heading}\n\n"
        f"{format_numbered_reservations(reservations)}\n\n"
        f"{guidance}"
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
