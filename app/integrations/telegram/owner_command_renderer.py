"""Allowlisted, plain-text Telegram rendering for owner ticket commands."""

import re

from app.integrations.telegram.message_utils import split_telegram_reply
from app.integrations.telegram.owner_notification_renderer import (
    CATEGORY_LABELS,
    PRIORITY_LABELS,
    STATUS_LABELS,
    format_telegram_timestamp,
)


GENERIC_COMMAND_REPLY = "Perintah tidak tersedia."
SERVICE_ERROR_REPLY = "Layanan tiket sementara tidak tersedia. Silakan coba lagi."
TICKET_UNAVAILABLE_REPLY = "Tiket tidak tersedia."
EMPTY_TICKETS_REPLY = "Tidak ada tiket bantuan aktif."
_SAFE_TICKET_NUMBER = re.compile(r"CS-[0-9]{4}-[0-9]{6,24}\Z")


def _safe_ticket_number(value) -> str:
    if isinstance(value, str) and _SAFE_TICKET_NUMBER.fullmatch(value):
        return value
    return "Nomor tersedia pada sistem"


def _detail(ticket, *, heading: str = "Detail tiket") -> str:
    return (
        f"{heading}\n\n"
        f"Nomor tiket: {_safe_ticket_number(ticket.ticket_number)}\n"
        f"Kategori: {CATEGORY_LABELS.get(ticket.category, 'Kategori bantuan')}\n"
        f"Prioritas: {PRIORITY_LABELS.get(ticket.priority, 'Prioritas tidak tersedia')}\n"
        f"Status: {STATUS_LABELS.get(ticket.status, 'Status tidak tersedia')}\n"
        f"Dibuat: {format_telegram_timestamp(ticket.created_at)}\n"
        f"Diperbarui: {format_telegram_timestamp(ticket.updated_at)}"
    )


def render_owner_command(command: str, result) -> list[str]:
    if result.code == "database_error":
        return split_telegram_reply(SERVICE_ERROR_REPLY)
    if result.code == "invalid_argument":
        usage = {
            "tickets": "Gunakan /tickets tanpa argumen.",
            "ticket": "Gunakan /ticket <nomor_tiket>.",
            "take": "Gunakan /take <nomor_tiket>.",
            "resolve": "Gunakan /resolve <nomor_tiket>.",
        }.get(command, GENERIC_COMMAND_REPLY)
        return split_telegram_reply(usage)
    if result.code in {"not_available", "closed"}:
        return split_telegram_reply(TICKET_UNAVAILABLE_REPLY)
    if result.code == "empty":
        return split_telegram_reply(EMPTY_TICKETS_REPLY)
    if result.code == "already_in_progress":
        return split_telegram_reply("Tiket tersebut sudah sedang ditangani.")
    if result.code == "already_resolved":
        return split_telegram_reply("Tiket tersebut sudah diselesaikan.")

    if command == "tickets" and result.code == "success":
        active = [
            ticket for ticket in result.tickets
            if ticket.status in {"open", "in_progress"}
        ][:10]
        if not active:
            return split_telegram_reply(EMPTY_TICKETS_REPLY)
        sections = ["Tiket bantuan aktif"]
        for index, ticket in enumerate(active, start=1):
            sections.append(_detail(ticket, heading=f"Tiket {index}"))
        return split_telegram_reply("\n\n".join(sections))

    if command == "ticket" and result.code == "success" and result.ticket is not None:
        return split_telegram_reply(_detail(result.ticket))
    if command == "take" and result.code == "success" and result.ticket is not None:
        number = _safe_ticket_number(result.ticket.ticket_number)
        return split_telegram_reply(f"Tiket {number} sekarang sedang ditangani.")
    if command == "resolve" and result.code == "success" and result.ticket is not None:
        number = _safe_ticket_number(result.ticket.ticket_number)
        return split_telegram_reply(f"Tiket {number} berhasil diselesaikan.")
    return split_telegram_reply(TICKET_UNAVAILABLE_REPLY)
