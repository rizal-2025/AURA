import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.models.support_ticket import SAFE_TICKET_SUMMARIES
from app.integrations.telegram.message_utils import split_telegram_reply


SAFE_SUMMARY_LIMIT = 500
CATEGORY_LABELS = {
    "explicit_human_request": "Permintaan bantuan petugas",
    "repeated_misunderstanding": "Percakapan tidak dipahami berulang",
    "repeated_invalid_input": "Input tidak valid berulang",
    "customer_frustration": "Pelanggan mengalami kendala",
    "ambiguous_intent": "Tindakan pelanggan perlu klarifikasi",
    "internal_error": "Kendala layanan internal",
}
PRIORITY_LABELS = {
    "urgent": "Mendesak",
    "high": "Tinggi",
    "medium": "Sedang",
    "low": "Rendah",
}
STATUS_LABELS = {
    "open": "Terbuka",
    "in_progress": "Sedang ditangani",
    "resolved": "Selesai",
    "closed": "Ditutup",
}
SUMMARY_LABELS = {
    "explicit_human_request": "Pelanggan meminta bantuan petugas.",
    "repeated_misunderstanding": "Percakapan tidak berhasil dipahami secara berulang.",
    "repeated_invalid_input": "Workflow menerima input tidak valid secara berulang.",
    "customer_frustration": "Pelanggan mengalami kendala dalam bantuan otomatis.",
    "ambiguous_intent": "Tindakan yang diminta memerlukan klarifikasi petugas.",
    "internal_error": "Kendala internal menghambat penyelesaian otomatis.",
}
MONTHS = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def _safe_label(mapping: dict[str, str], value, fallback: str) -> str:
    return mapping.get(value, fallback) if isinstance(value, str) else fallback


def _safe_summary(ticket) -> str:
    category = getattr(ticket, "category", None)
    persisted = getattr(ticket, "safe_summary", None)
    if persisted != SAFE_TICKET_SUMMARIES.get(category):
        summary = "Ringkasan operasional tersedia."
    else:
        summary = SUMMARY_LABELS.get(category, "Ringkasan operasional tersedia.")
    summary = re.sub(r"[\x00-\x1f\x7f]", " ", summary)
    summary = " ".join(summary.split())
    return summary[:SAFE_SUMMARY_LIMIT]


def _format_created_at(value) -> str:
    if not isinstance(value, datetime):
        return "Waktu tersedia pada sistem"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        local = aware.astimezone(ZoneInfo("Asia/Jakarta"))
        suffix = "WIB"
    except ZoneInfoNotFoundError:
        local = aware.astimezone(timezone.utc)
        suffix = "UTC"
    return f"{local.day} {MONTHS[local.month - 1]} {local.year} {local:%H:%M} {suffix}"


def render_owner_notification(ticket) -> list[str]:
    number = getattr(ticket, "ticket_number", None)
    if not isinstance(number, str) or not re.fullmatch(r"CS-[0-9]{4}-[0-9]{6,}", number):
        number = "Nomor tersedia pada sistem"
    message = (
        "Tiket bantuan baru\n\n"
        f"Nomor tiket: {number}\n"
        f"Kategori: {_safe_label(CATEGORY_LABELS, getattr(ticket, 'category', None), 'Bantuan pelanggan')}\n"
        f"Prioritas: {_safe_label(PRIORITY_LABELS, getattr(ticket, 'priority', None), 'Perlu ditinjau')}\n"
        f"Status: {_safe_label(STATUS_LABELS, getattr(ticket, 'status', None), 'Perlu ditinjau')}\n"
        f"Waktu dibuat: {_format_created_at(getattr(ticket, 'created_at', None))}\n"
        f"Ringkasan: {_safe_summary(ticket)}"
    )
    return split_telegram_reply(message)

