"""Deterministic, locale-aware reservation selection presentation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.brain.reservation_entity_extractor import (
    PublicReferenceParseStatus,
    parse_public_reservation_reference,
)
from app.core.locale import (
    SupportedLocale,
    current_locale,
    format_date,
    format_time,
)


NEXT_PAGE_COMMAND = "berikutnya"
FIRST_PAGE_COMMAND = "awal"
_NEXT_PAGE_COMMANDS = frozenset({NEXT_PAGE_COMMAND, "next", "next page"})
_FIRST_PAGE_COMMANDS = frozenset({FIRST_PAGE_COMMAND, "first", "first page"})


@dataclass(frozen=True)
class ReservationSelection:
    status: str
    reference: str | None = None


def parse_reservation_selection(
    value: str,
    candidate_references: tuple[str, ...],
) -> ReservationSelection:
    """Resolve a displayed number or backwards-compatible public reference."""

    normalized = value.strip() if isinstance(value, str) else ""
    command = " ".join(normalized.casefold().split())
    if command in _NEXT_PAGE_COMMANDS:
        return ReservationSelection("next_page")
    if command in _FIRST_PAGE_COMMANDS:
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
    if current_locale() is SupportedLocale.EN_US:
        return (
            f"Name: {reservation.name}\n"
            f"Date: {format_date(reservation.date)}\n"
            f"Time: {format_time(reservation.time)}\n"
            f"Party size: {reservation.people} people"
        )
    return (
        f"Nama: {reservation.name}\n"
        f"Tanggal: {format_date(reservation.date)}\n"
        f"Waktu: {format_time(reservation.time)}\n"
        f"Jumlah orang: {reservation.people} orang"
    )


def format_numbered_reservations(reservations) -> str:
    people_label = (
        "people" if current_locale() is SupportedLocale.EN_US else "orang"
    )
    return "\n".join(
        f"{index}. {format_date(reservation.date, abbreviated=True)} · "
        f"{format_time(reservation.time)} · {reservation.people} {people_label}"
        for index, reservation in enumerate(reservations, start=1)
    )


def format_paginated_selection(
    reservations,
    *,
    has_more: bool,
    is_later_page: bool,
) -> str:
    count = len(reservations)
    if current_locale() is SupportedLocale.EN_US:
        heading = (
            "More reservations:"
            if is_later_page
            else "I found several reservations:"
            if has_more
            else f"I found {count} reservations:"
        )
        if not has_more and not is_later_page:
            guidance = f"Choose a reservation from 1 to {count}."
        else:
            guidance_lines = [f"Enter 1 to {count} to choose."]
            if has_more:
                guidance_lines.append('Enter "next" to see more reservations.')
            if is_later_page:
                guidance_lines.append(
                    'Enter "first" to return to the first page.'
                )
            guidance = "\n".join(guidance_lines)
    else:
        heading = (
            "Reservasi lainnya:"
            if is_later_page
            else "Saya menemukan beberapa reservasi:"
            if has_more
            else f"Saya menemukan {count} reservasi:"
        )
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
