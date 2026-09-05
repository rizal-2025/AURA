"""Strict, request-scoped presentation locale support.

Domain and workflow values remain canonical English.  Only rendering helpers
read this context, so changing language cannot mutate conversation state.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time
from enum import Enum
from typing import Iterator


class SupportedLocale(str, Enum):
    ID_ID = "id-ID"
    EN_US = "en-US"


DEFAULT_LOCALE = SupportedLocale.ID_ID
_current_locale: ContextVar[SupportedLocale] = ContextVar(
    "aura_presentation_locale",
    default=DEFAULT_LOCALE,
)


def resolve_locale(value: object) -> SupportedLocale:
    """Resolve only the two supported enum values and fail closed otherwise."""

    if isinstance(value, SupportedLocale):
        return value
    if type(value) is str:
        try:
            return SupportedLocale(value)
        except ValueError:
            pass
    raise ValueError("UNSUPPORTED_LOCALE")


def current_locale() -> SupportedLocale:
    return _current_locale.get()


def response_language_instruction() -> str:
    return (
        "Respond in natural American English."
        if current_locale() is SupportedLocale.EN_US
        else "Jawab dalam bahasa Indonesia yang alami."
    )


@contextmanager
def presentation_locale(locale: SupportedLocale) -> Iterator[None]:
    token = _current_locale.set(resolve_locale(locale))
    try:
        yield
    finally:
        _current_locale.reset(token)


_TEXT: dict[str, dict[SupportedLocale, str]] = {
    "authorization_required": {
        SupportedLocale.ID_ID: "Identitas pelanggan tidak valid atau telah kedaluwarsa.",
        SupportedLocale.EN_US: "Your customer identity is invalid or has expired.",
    },
    "unknown_request": {
        SupportedLocale.ID_ID: "Maaf, saya belum memahami permintaan Anda. Bisa dijelaskan kembali?",
        SupportedLocale.EN_US: "Sorry, I didn't understand your request. Could you explain it another way?",
    },
    "general_conversation_unavailable": {
        SupportedLocale.ID_ID: "Maaf, saya sedang tidak dapat menjawab percakapan umum. Silakan coba lagi.",
        SupportedLocale.EN_US: "Sorry, I can't answer general conversation right now. Please try again.",
    },
    "ambiguous_action": {
        SupportedLocale.ID_ID: "Apakah Anda ingin mengubah atau membatalkan reservasi?",
        SupportedLocale.EN_US: "Would you like to update or cancel a reservation?",
    },
    "ambiguous_reservation": {
        SupportedLocale.ID_ID: "Saya belum yakin tindakan reservasi yang Anda maksud. Mohon jelaskan kembali.",
        SupportedLocale.EN_US: "I'm not sure which reservation action you mean. Please clarify.",
    },
    "greeting": {
        SupportedLocale.ID_ID: "Halo! Saya AURA. Ada yang bisa saya bantu?",
        SupportedLocale.EN_US: "Hello! I'm AURA. How can I help?",
    },
    "general_help": {
        SupportedLocale.ID_ID: "Saya akan membantu menjawab pertanyaan Anda.",
        SupportedLocale.EN_US: "I’ll help answer your question.",
    },
    "check_reservation_help": {
        SupportedLocale.ID_ID: "Saya akan membantu mengecek reservasi Anda.",
        SupportedLocale.EN_US: "I’ll help check your reservation.",
    },
    "cancel_reservation_help": {
        SupportedLocale.ID_ID: "Saya akan membantu membatalkan reservasi Anda.",
        SupportedLocale.EN_US: "I’ll help cancel your reservation.",
    },
    "no_steps": {
        SupportedLocale.ID_ID: "Tidak ada langkah yang dapat dijalankan.",
        SupportedLocale.EN_US: "There are no available steps to run.",
    },
    "unsupported_agent": {
        SupportedLocale.ID_ID: "Tidak ada agent yang tersedia untuk intent ini.",
        SupportedLocale.EN_US: "No agent is available for this request.",
    },
    "handoff_simulated": {
        SupportedLocale.ID_ID: "Permintaan bantuan admin telah disimulasikan pada demo ini.",
        SupportedLocale.EN_US: "An admin-assistance request was simulated for this demo.",
    },
    "handoff_none": {
        SupportedLocale.ID_ID: "Tidak ada simulasi bantuan admin yang aktif.",
        SupportedLocale.EN_US: "There is no active simulated admin-assistance request.",
    },
    "no_reservations": {
        SupportedLocale.ID_ID: "Belum ada reservasi.",
        SupportedLocale.EN_US: "You don't have any reservations yet.",
    },
    "reservation_list": {
        SupportedLocale.ID_ID: "Daftar reservasi terbaru:\n\n{records}",
        SupportedLocale.EN_US: "Your latest reservations:\n\n{records}",
    },
    "reference_unavailable": {
        SupportedLocale.ID_ID: "Data reservasi belum dapat diproses dengan aman. Silakan coba lagi nanti.",
        SupportedLocale.EN_US: "Reservation data cannot be displayed safely right now. Please try again later.",
    },
    "reference_unavailable_view": {
        SupportedLocale.ID_ID: "Data reservasi belum dapat ditampilkan dengan aman. Silakan coba lagi nanti.",
        SupportedLocale.EN_US: "Reservation data cannot be displayed safely right now. Please try again later.",
    },
    "reference_not_found": {
        SupportedLocale.ID_ID: "Referensi reservasi tidak ditemukan.",
        SupportedLocale.EN_US: "That reservation was not found or is unavailable.",
    },
    "reference_ambiguous": {
        SupportedLocale.ID_ID: "Kirim tepat satu referensi reservasi.",
        SupportedLocale.EN_US: "The reservation reference is ambiguous. Choose one of the displayed reservation numbers.",
    },
    "update_none": {
        SupportedLocale.ID_ID: "Saya tidak menemukan reservasi aktif yang dapat diubah.",
        SupportedLocale.EN_US: "I couldn't find an active reservation to update.",
    },
    "cancel_none": {
        SupportedLocale.ID_ID: "Saya tidak menemukan reservasi aktif yang dapat dibatalkan.",
        SupportedLocale.EN_US: "I couldn't find an active reservation to cancel.",
    },
    "select_update_single": {
        SupportedLocale.ID_ID: "Saya menemukan reservasi ini:\n\n{summary}\n\nApakah ini reservasi yang ingin diubah? Ya / Tidak",
        SupportedLocale.EN_US: "I found this reservation:\n\n{summary}\n\nIs this the reservation you want to update? Yes / No",
    },
    "select_cancel_single": {
        SupportedLocale.ID_ID: "Saya menemukan reservasi ini:\n\n{summary}\n\nApakah ini reservasi yang ingin dibatalkan? Ya / Tidak",
        SupportedLocale.EN_US: "I found this reservation:\n\n{summary}\n\nIs this the reservation you want to cancel? Yes / No",
    },
    "selected_update": {
        SupportedLocale.ID_ID: "Reservasi dipilih:\n\n{summary}\n\nBagian mana yang ingin diubah?\nPilih: nama, jumlah orang, tanggal, atau waktu.",
        SupportedLocale.EN_US: "Reservation selected:\n\n{summary}\n\nWhich detail would you like to change?\nChoose: name, number of people, date, or time.",
    },
    "choose_update_field": {
        SupportedLocale.ID_ID: "Bagian mana yang ingin diubah?\nPilih: nama, jumlah orang, tanggal, atau waktu.",
        SupportedLocale.EN_US: "Which detail would you like to change?\nChoose: name, number of people, date, or time.",
    },
    "invalid_update_field": {
        SupportedLocale.ID_ID: "Pilihan tidak dikenali. Pilih: nama, jumlah orang, tanggal, atau waktu.",
        SupportedLocale.EN_US: "That choice isn't recognized. Choose: name, number of people, date, or time.",
    },
    "update_stopped": {
        SupportedLocale.ID_ID: "Baik, proses perubahan reservasi dihentikan. Tidak ada perubahan.",
        SupportedLocale.EN_US: "Okay, I stopped the update. No changes were made.",
    },
    "update_yes_no": {
        SupportedLocale.ID_ID: "Mohon jawab Ya atau Tidak. Apakah ini reservasi yang ingin diubah?",
        SupportedLocale.EN_US: "Please answer Yes or No. Is this the reservation you want to update?",
    },
    "update_session_invalid": {
        SupportedLocale.ID_ID: "Sesi perubahan tidak valid. Mulai lagi dengan 'ubah reservasi saya'.",
        SupportedLocale.EN_US: "The update flow is no longer valid. Start again with 'update my reservation'.",
    },
    "update_success": {
        SupportedLocale.ID_ID: "Reservasi berhasil diperbarui:\n\n{reservation}",
        SupportedLocale.EN_US: "Reservation updated successfully:\n\n{reservation}",
    },
    "update_stale": {
        SupportedLocale.ID_ID: "Reservasi yang dipilih tidak lagi tersedia untuk diubah.\n\n{selection}",
        SupportedLocale.EN_US: "The selected reservation is no longer available to update.\n\n{selection}",
    },
    "cancel_selected": {
        SupportedLocale.ID_ID: "Reservasi dipilih:\n\n{summary}\n\nYakin ingin membatalkan reservasi ini? Ya / Tidak",
        SupportedLocale.EN_US: "Reservation selected:\n\n{summary}\n\nAre you sure you want to cancel this reservation? Yes / No",
    },
    "cancel_selection_rejected": {
        SupportedLocale.ID_ID: "Baik, pilihan reservasi dibatalkan. Tidak ada perubahan pada reservasi.",
        SupportedLocale.EN_US: "Okay, I cleared that selection. No reservation was changed.",
    },
    "cancel_yes_no_selection": {
        SupportedLocale.ID_ID: "Mohon jawab Ya atau Tidak. Apakah ini reservasi yang ingin dibatalkan?",
        SupportedLocale.EN_US: "Please answer Yes or No. Is this the reservation you want to cancel?",
    },
    "cancel_flow_stopped": {
        SupportedLocale.ID_ID: "Pembatalan reservasi dibatalkan. Tidak ada perubahan pada reservasi.",
        SupportedLocale.EN_US: "The cancellation was stopped. No reservation was changed.",
    },
    "cancel_session_invalid": {
        SupportedLocale.ID_ID: "Sesi pembatalan tidak valid. Mulai lagi dengan 'batalkan reservasi saya'.",
        SupportedLocale.EN_US: "The cancellation flow is no longer valid. Start again with 'cancel my reservation'.",
    },
    "cancel_confirm": {
        SupportedLocale.ID_ID: "Yakin ingin membatalkan reservasi ini? Ya / Tidak",
        SupportedLocale.EN_US: "Are you sure you want to cancel this reservation? Yes / No",
    },
    "cancel_already": {
        SupportedLocale.ID_ID: "Reservasi ini sudah dibatalkan. Tidak ada perubahan tambahan.",
        SupportedLocale.EN_US: "This reservation has already been cancelled. No additional changes were made.",
    },
    "cancel_success": {
        SupportedLocale.ID_ID: "Reservasi berhasil dibatalkan:\n\n{reservation}",
        SupportedLocale.EN_US: "Reservation cancelled successfully:\n\n{reservation}",
    },
    "cancel_stale": {
        SupportedLocale.ID_ID: "Reservasi yang dipilih tidak lagi tersedia untuk dibatalkan.\n\n{selection}",
        SupportedLocale.EN_US: "The selected reservation is no longer available to cancel.\n\n{selection}",
    },
    "no_next_page": {
        SupportedLocale.ID_ID: "Tidak ada reservasi berikutnya. Pilih nomor pada halaman ini{guidance}.",
        SupportedLocale.EN_US: "There are no more reservations. Choose a number on this page{guidance}.",
    },
    "already_first_page": {
        SupportedLocale.ID_ID: "Anda sudah berada di daftar awal. Pilih nomor reservasi.",
        SupportedLocale.EN_US: "You're already on the first page. Choose a reservation number.",
    },
    "invalid_selection": {
        SupportedLocale.ID_ID: "Pilihan tidak valid. Masukkan angka 1 sampai {count}{guidance}.",
        SupportedLocale.EN_US: "That choice isn't valid. Enter a number from 1 to {count}{guidance}.",
    },
    "selection_navigation": {
        SupportedLocale.ID_ID: ", atau ketik {commands}",
        SupportedLocale.EN_US: ", or enter {commands}",
    },
    "return_to_first": {
        SupportedLocale.ID_ID: " atau ketik \"awal\" untuk kembali ke daftar awal",
        SupportedLocale.EN_US: " or enter \"first\" to return to the first page",
    },
    "ask_name": {
        SupportedLocale.ID_ID: "Atas nama siapa reservasinya?",
        SupportedLocale.EN_US: "What name should I use for the reservation?",
    },
    "ask_people": {
        SupportedLocale.ID_ID: "Untuk berapa orang?",
        SupportedLocale.EN_US: "How many people is the reservation for?",
    },
    "ask_date": {
        SupportedLocale.ID_ID: "Tanggal berapa?",
        SupportedLocale.EN_US: "What date would you like?",
    },
    "ask_time": {
        SupportedLocale.ID_ID: "Jam berapa?",
        SupportedLocale.EN_US: "What time would you like?",
    },
    "ask_new_name": {
        SupportedLocale.ID_ID: "Nama baru menjadi siapa?",
        SupportedLocale.EN_US: "What should the new name be?",
    },
    "ask_new_people": {
        SupportedLocale.ID_ID: "Jumlah orang baru menjadi berapa?",
        SupportedLocale.EN_US: "What should the new party size be?",
    },
    "ask_new_date": {
        SupportedLocale.ID_ID: "Tanggal baru menjadi kapan?",
        SupportedLocale.EN_US: "What should the new date be?",
    },
    "ask_new_time": {
        SupportedLocale.ID_ID: "Waktu baru menjadi jam berapa?",
        SupportedLocale.EN_US: "What should the new time be?",
    },
    "invalid_people": {
        SupportedLocale.ID_ID: "Jumlah orang harus berupa angka positif. Silakan masukkan jumlah orang yang valid.",
        SupportedLocale.EN_US: "The party size must be a number from 1 to 20.",
    },
    "unclear_date": {
        SupportedLocale.ID_ID: "Tanggal belum jelas. Sebutkan tanggal lengkap, misalnya 30 Juli 2026.",
        SupportedLocale.EN_US: "The date isn't clear. Please provide a complete date, for example August 30, 2026.",
    },
    "past_reservation_date": {
        SupportedLocale.ID_ID: "Tanggal reservasi tersebut sudah lewat. Silakan pilih tanggal hari ini atau tanggal setelahnya.",
        SupportedLocale.EN_US: "That reservation date has already passed. Please choose today or a future date.",
    },
    "unclear_time": {
        SupportedLocale.ID_ID: "Waktu belum jelas. Sebutkan jam, misalnya 19.30.",
        SupportedLocale.EN_US: "The time isn't clear. Please provide a time, for example 7:30 PM.",
    },
    "create_confirmation": {
        SupportedLocale.ID_ID: "Baik, saya konfirmasi reservasi Anda:\n\nNama: {name}\nJumlah: {people} orang\nTanggal: {date}\nJam: {time}\n\nApakah data ini sudah benar? Ya / Tidak",
        SupportedLocale.EN_US: "Please confirm your reservation:\n\nName: {name}\nParty size: {people}\nDate: {date}\nTime: {time}\n\nIs everything correct? Yes / No",
    },
    "create_success": {
        SupportedLocale.ID_ID: "Reservasi berhasil dibuat.\n\nReferensi reservasi: {reference}\n\nAnda dapat melihat, mengubah, atau membatalkan reservasi langsung dari sesi demo ini tanpa memasukkan referensi secara manual.",
        SupportedLocale.EN_US: "Reservation created successfully.\n\nReservation reference: {reference}\n\nYou can view, update, or cancel reservations directly in this demo session without entering the reference manually.",
    },
    "create_rejected": {
        SupportedLocale.ID_ID: "Baik, reservasi tidak dilanjutkan.",
        SupportedLocale.EN_US: "Okay, I won't create the reservation.",
    },
    "no_available_step": {
        SupportedLocale.ID_ID: "Tidak ada langkah yang tersedia.",
        SupportedLocale.EN_US: "There are no available steps.",
    },
    "reservation_complete": {
        SupportedLocale.ID_ID: "Data reservasi sudah lengkap.",
        SupportedLocale.EN_US: "The reservation details are complete.",
    },
    "reservation_ready": {
        SupportedLocale.ID_ID: "Reservasi siap disimpan.",
        SupportedLocale.EN_US: "The reservation is ready to be saved.",
    },
    "customer_unavailable": {
        SupportedLocale.ID_ID: "Identitas pelanggan tidak tersedia. Silakan coba lagi.",
        SupportedLocale.EN_US: "Your customer identity is unavailable. Please try again.",
    },
    "reservation_service_unavailable": {
        SupportedLocale.ID_ID: "Layanan reservasi belum tersedia. Silakan coba lagi.",
        SupportedLocale.EN_US: "The reservation service is unavailable. Please try again.",
    },
    "ask_edit_name": {
        SupportedLocale.ID_ID: "Baik, nama menjadi siapa?",
        SupportedLocale.EN_US: "Okay, what should the name be?",
    },
    "ask_edit_people": {
        SupportedLocale.ID_ID: "Baik, jumlah orang menjadi berapa?",
        SupportedLocale.EN_US: "Okay, what should the party size be?",
    },
    "ask_edit_date": {
        SupportedLocale.ID_ID: "Baik, tanggal menjadi kapan?",
        SupportedLocale.EN_US: "Okay, what should the date be?",
    },
    "ask_edit_time": {
        SupportedLocale.ID_ID: "Baik, jam menjadi berapa?",
        SupportedLocale.EN_US: "Okay, what should the time be?",
    },
    "complete_reservation_details": {
        SupportedLocale.ID_ID: "Mohon lengkapi data reservasi.",
        SupportedLocale.EN_US: "Please complete the reservation details.",
    },
    "clarify_date_parts": {
        SupportedLocale.ID_ID: "Tanggal {day} bulan dan tahun berapa?",
        SupportedLocale.EN_US: "Which month and year should I use for the {day}th?",
    },
    "clarify_day_period": {
        SupportedLocale.ID_ID: "Pukul tersebut pagi atau malam? Contoh: 07.00 atau 19.00.",
        SupportedLocale.EN_US: "Is that time in the morning or evening? For example, 7:00 AM or 7:00 PM.",
    },
    "persistence_uncertain": {
        SupportedLocale.ID_ID: "Maaf, perubahan belum dapat dipastikan. Silakan periksa status lalu coba lagi.",
        SupportedLocale.EN_US: "Sorry, the change could not be confirmed. Check the status before trying again.",
    },
    "committed_state_unavailable": {
        SupportedLocale.ID_ID: "Proses telah selesai, tetapi status percakapan tidak dapat diperbarui. Silakan cek daftar reservasi sebelum mencoba lagi.",
        SupportedLocale.EN_US: "The operation completed, but the conversation state could not be updated. Check your reservations before trying again.",
    },
    "committed_format_fallback": {
        SupportedLocale.ID_ID: "Proses reservasi telah selesai, tetapi konfirmasi rinci tidak dapat ditampilkan. Silakan lihat daftar reservasi Anda untuk memverifikasi status.",
        SupportedLocale.EN_US: "The reservation operation completed, but detailed confirmation cannot be displayed. Check your reservation list to verify the status.",
    },
}


def tr(key: str, **values: object) -> str:
    template = _TEXT[key][current_locale()]
    return template.format(**values)


def status_label(status: object) -> str:
    canonical = str(status).casefold()
    labels = {
        "pending": {
            SupportedLocale.ID_ID: "Menunggu",
            SupportedLocale.EN_US: "Pending",
        },
        "cancelled": {
            SupportedLocale.ID_ID: "Dibatalkan",
            SupportedLocale.EN_US: "Cancelled",
        },
        "confirmed": {
            SupportedLocale.ID_ID: "Dikonfirmasi",
            SupportedLocale.EN_US: "Confirmed",
        },
        "completed": {
            SupportedLocale.ID_ID: "Selesai",
            SupportedLocale.EN_US: "Completed",
        },
    }
    return labels.get(canonical, {}).get(current_locale(), str(status))


def format_date(value: object, *, abbreviated: bool = False) -> str:
    raw = str(value)
    try:
        parsed = date.fromisoformat(raw[:10])
    except ValueError:
        return raw
    if current_locale() is SupportedLocale.EN_US:
        month = parsed.strftime("%b" if abbreviated else "%B")
        return f"{month} {parsed.day}, {parsed.year}"
    months = (
        "",
        "Jan" if abbreviated else "Januari",
        "Feb" if abbreviated else "Februari",
        "Mar" if abbreviated else "Maret",
        "Apr" if abbreviated else "April",
        "Mei",
        "Jun" if abbreviated else "Juni",
        "Jul" if abbreviated else "Juli",
        "Agu" if abbreviated else "Agustus",
        "Sep" if abbreviated else "September",
        "Okt" if abbreviated else "Oktober",
        "Nov" if abbreviated else "November",
        "Des" if abbreviated else "Desember",
    )
    return f"{parsed.day} {months[parsed.month]} {parsed.year}"


def format_time(value: object) -> str:
    raw = str(value)
    try:
        parsed = time.fromisoformat(raw)
    except ValueError:
        return raw
    if current_locale() is SupportedLocale.EN_US:
        hour = parsed.hour % 12 or 12
        suffix = "AM" if parsed.hour < 12 else "PM"
        return f"{hour}:{parsed.minute:02d} {suffix}"
    return f"{parsed.hour:02d}.{parsed.minute:02d}"


def format_reservation(reservation: object, *, include_reference: bool = True) -> str:
    locale = current_locale()
    labels = (
        {
            "reference": "Referensi reservasi",
            "name": "Nama",
            "people": "Jumlah orang",
            "date": "Tanggal",
            "time": "Waktu",
            "status": "Status",
            "people_suffix": "orang",
        }
        if locale is SupportedLocale.ID_ID
        else {
            "reference": "Reservation reference",
            "name": "Name",
            "people": "Party size",
            "date": "Date",
            "time": "Time",
            "status": "Status",
            "people_suffix": "people",
        }
    )
    lines: list[str] = []
    if include_reference:
        lines.append(f"{labels['reference']}: {getattr(reservation, 'reference')}")
    lines.extend(
        (
            f"{labels['name']}: {getattr(reservation, 'name')}",
            f"{labels['people']}: {getattr(reservation, 'people')} {labels['people_suffix']}",
            f"{labels['date']}: {format_date(getattr(reservation, 'date'))}",
            f"{labels['time']}: {format_time(getattr(reservation, 'time'))}",
            f"{labels['status']}: {status_label(getattr(reservation, 'status'))}",
        )
    )
    return "\n".join(lines)
