"""Bounded, deterministic Indonesian natural-language helpers.

These helpers only canonicalize complete tokens and explicitly supported
phrases.  They never mutate the original customer text or a captured name.
"""

from __future__ import annotations

import re
import unicodedata

from app.core.input_validation import (
    InputValidationError,
    validate_reservation_people,
)


_TOKEN_REPLACEMENTS = {
    "ga": "tidak",
    "gak": "tidak",
    "nga": "tidak",
    "ngga": "tidak",
    "nggak": "tidak",
    "enggak": "tidak",
    "mo": "ingin",
    "mau": "ingin",
    "pengen": "ingin",
    "malem": "malam",
    "rubah": "ubah",
    "pesen": "pesan",
    "booking": "reservasi",
    "bookingnya": "reservasinya",
    "jadinya": "jadi",
}
_AFFIX_REPLACEMENTS = {
    "mengubah": "ubah",
    "diubah": "ubah",
    "mengganti": "ganti",
    "diganti": "ganti",
    "batalkan": "batal",
    "membatalkan": "batal",
    "dibatalkan": "batal",
    "batalin": "batal",
    "ditambah": "tambah",
    "dikurangi": "kurang",
    "diedit": "edit",
    "direvisi": "revisi",
    "digeser": "geser",
    "melihat": "lihat",
    "sambungkan": "hubungkan",
}

NUMBER_WORDS = {
    "satu": 1,
    "seorang": 1,
    "dua": 2,
    "tiga": 3,
    "empat": 4,
    "lima": 5,
    "enam": 6,
    "tujuh": 7,
    "delapan": 8,
    "sembilan": 9,
    "sepuluh": 10,
    "sebelas": 11,
    "dua belas": 12,
    "tiga belas": 13,
    "empat belas": 14,
    "lima belas": 15,
    "enam belas": 16,
    "tujuh belas": 17,
    "delapan belas": 18,
    "sembilan belas": 19,
    "dua puluh": 20,
}

POSITIVE_CONFIRMATIONS = frozenset(
    {
        "ya",
        "iya",
        "iya benar",
        "yes",
        "yes please",
        "benar",
        "sudah benar",
        "betul",
        "oke",
        "ok",
        "okay",
        "oke lanjut",
        "ok lanjut",
        "ya lanjut",
        "iya lanjut",
        "oke gas",
        "sip",
        "sip lanjut",
        "betul lanjutkan",
        "silakan lanjutkan",
        "gas",
        "lanjut",
        "lanjutkan",
        "boleh lanjut",
        "setuju",
        "sesuai",
        "pas",
    }
)
NEGATIVE_CONFIRMATIONS = frozenset(
    {
        "tidak",
        "bukan",
        "salah",
        "no",
        "nope",
        "no thanks",
        "jangan",
        "jangan lanjut",
        "batal",
        "batal aja",
        "batal saja",
        "tidak usah",
        "tidak jadi",
        "sudah tidak perlu",
        "jangan diproses",
        "tidak jadi pesan",
        "tolong hapus pesanan meja",
        "tolong cancel reservasi saya",
    }
)
NEGATIVE_CONFIRMATION_WORDS = frozenset({"tidak", "jangan", "batal"})

GREETING_PHRASES = frozenset(
    {
        "halo",
        "hai",
        "hi",
        "hello",
        "hello aura",
        "hi aura",
        "good morning",
        "good afternoon",
        "good evening",
        "halo min",
        "hai min",
        "pagi",
        "selamat pagi",
        "selamat siang",
        "selamat sore",
        "selamat malam",
        "permisi",
        "halo aura",
        "pagi aura",
        "siang aura",
        "sore aura",
        "malam aura",
    }
)

CREATE_RESERVATION_PHRASES = frozenset(
    {
        "ingin reservasi",
        "ingin pesan meja",
        "pesan meja",
        "tolong reservasi meja",
        "tolong reservasi",
        "bisa pesan meja",
        "ada meja untuk besok",
        "ingin reservasi buat keluarga",
        "tolong siapkan meja",
        "book a table",
        "book me a table",
        "make a reservation",
        "i want to make a reservation",
        "i would like a reservation",
    }
)

UPDATE_RESERVATION_PHRASES = frozenset(
    {
        "ingin ubah reservasi",
        "ingin ganti reservasi",
        "jadwalnya ingin saya ganti",
        "tolong ubah pesanan saya",
        "reservasi saya perlu revisi",
        "saya ingin pindah jadwal",
        "reservasinya ingin edit",
        "ada perubahan untuk reservasi saya",
        "jadwalnya geser",
        "ubah waktu reservasi saya",
        "update my reservation",
        "change my reservation",
        "edit my reservation",
    }
)

CANCEL_RESERVATION_PHRASES = frozenset(
    {
        "ingin batal reservasi",
        "batal reservasi saya",
        "reservasinya batal aja",
        "saya tidak jadi datang",
        "saya tidak jadi hadir",
        "tolong hapus pesanan meja",
        "reservasinya tidak jadi",
        "batal reservasinya",
        "saya ingin batal pesanan",
        "tidak jadi pakai reservasinya",
        "tolong cancel reservasi saya",
        "cancel my reservation",
        "cancel the reservation",
    }
)

NEGATED_CANCEL_RESERVATION_PHRASES = frozenset(
    {
        "saya tidak jadi datang",
        "saya tidak jadi hadir",
        "reservasinya tidak jadi",
        "tidak jadi pakai reservasinya",
    }
)

VIEW_RESERVATION_PHRASES = frozenset(
    {
        "cek reservasi saya",
        "lihat reservasi saya",
        "reservasi saya masih ada",
        "reservasi saya sudah tercatat",
        "pesanan saya masuk belum",
        "tampilkan reservasi saya",
        "saya punya reservasi apa saja",
        "cek jadwal saya",
        "lihat pesanan meja saya",
        "reservasi besok masih aktif",
        "nomor reservasi saya berapa",
        "show my reservation",
        "show my reservations",
        "view my reservation",
        "view my reservations",
        "list my reservations",
        "check my reservation",
    }
)

FIELD_ALIASES = {
    "name": frozenset({"name", "nama", "namanya", "atas nama"}),
    "people": frozenset(
        {
            "people",
            "number of people",
            "party size",
            "jumlah",
            "jumlah orang",
            "orang",
            "orangnya",
            "peserta",
        }
    ),
    "date": frozenset(
        {"date", "tanggal", "tanggalnya", "hari", "jadwal", "jadwalnya"}
    ),
    "time": frozenset(
        {"time", "jam", "jamnya", "pukul", "waktu", "waktunya"}
    ),
}


def normalize_indonesian_text(value: str) -> str:
    """Return a punctuation-tolerant canonical form without substring edits."""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value).casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    canonical = [
        _AFFIX_REPLACEMENTS.get(
            word,
            _TOKEN_REPLACEMENTS.get(word, word),
        )
        for word in words
    ]
    return " ".join(canonical)


def contains_bounded_phrase(
    normalized: str,
    phrases: frozenset[str],
) -> bool:
    """Match one canonical phrase at whitespace-delimited boundaries."""
    if not normalized:
        return False
    return any(
        re.search(
            rf"(?:^|\s){re.escape(phrase)}(?:$|\s)",
            normalized,
        )
        is not None
        for phrase in phrases
    )


def parse_confirmation(value: str) -> str | None:
    """Return ``confirm`` or ``reject`` for a bounded supported response."""
    normalized = normalize_indonesian_text(value)
    tokens = frozenset(normalized.split())
    # Rejection is authoritative even when the same response also contains a
    # positive cue, for example "jangan lanjut" or "tidak jadi lanjut".
    if (
        normalized in NEGATIVE_CONFIRMATIONS
        or tokens.intersection(NEGATIVE_CONFIRMATION_WORDS)
    ):
        return "reject"
    if normalized in POSITIVE_CONFIRMATIONS:
        return "confirm"
    return None


def _number_word_value(value: str) -> int | None:
    normalized = normalize_indonesian_text(value)
    return NUMBER_WORDS.get(normalized)


def parse_people_count(value: str, *, allow_bare: bool = True) -> int | None:
    """Parse one safe people count and enforce the existing 1..20 limit."""
    if not isinstance(value, str):
        return None
    normalized = normalize_indonesian_text(value)
    if not normalized:
        return None
    if re.search(r"(?<![0-9])-\s*[0-9]+", value):
        return None
    if re.search(r"[0-9]+[.,][0-9]+", value):
        return None
    if re.search(
        r"\b(?:[0-9]+|[a-z]+)\s+(?:atau|dan)\s+(?:[0-9]+|[a-z]+)(?:\s+orang)?\b",
        normalized,
    ):
        return None

    candidate: int | None = None
    if allow_bare and re.fullmatch(r"[0-9]+", normalized):
        candidate = int(normalized)
    elif allow_bare and normalized in NUMBER_WORDS:
        candidate = NUMBER_WORDS[normalized]
    else:
        patterns = (
            r"\b(?:untuk|buat|meja untuk)\s+([0-9]+)\s*(?:orang)?\b",
            r"\b([0-9]+)\s+orang\b",
            r"\b(?:for|party of)\s+([0-9]+)\s*(?:people|persons|guests)?\b",
            r"\b([0-9]+)\s+(?:people|persons|guests)\b",
            r"\b(?:jadi|menjadi|ke)\s+([0-9]+)\b",
        )
        numeric_matches: list[str] = []
        for pattern in patterns:
            numeric_matches.extend(re.findall(pattern, normalized))
        numeric_values = {int(item) for item in numeric_matches}
        if len(numeric_values) == 1:
            candidate = numeric_values.pop()
        elif len(numeric_values) > 1:
            return None

        if candidate is None:
            for prefix, number in (
                ("berdua", 2),
                ("bertiga", 3),
                ("berempat", 4),
                ("berlima", 5),
                ("berenam", 6),
            ):
                if re.search(
                    rf"\b(?:(?:kami|cuma|hanya)\s+)?{prefix}\b",
                    normalized,
                ):
                    candidate = number
                    break

        if candidate is None:
            word_pattern = "|".join(
                sorted(
                    (re.escape(word) for word in NUMBER_WORDS),
                    key=len,
                    reverse=True,
                )
            )
            match = re.search(
                rf"\b(?:untuk|buat|meja untuk|jadi|menjadi|ke)\s+({word_pattern})(?:\s+orang)?\b",
                normalized,
            )
            if match is None:
                match = re.search(rf"\b({word_pattern})\s+orang\b", normalized)
            if match:
                candidate = _number_word_value(match.group(1))

    if candidate is None:
        return None
    try:
        return validate_reservation_people(candidate)
    except InputValidationError:
        return None


def parse_target_field(value: str) -> str | None:
    """Resolve one allowlisted editable field from natural update wording."""
    normalized = normalize_indonesian_text(value)
    matches = {
        field
        for field, aliases in FIELD_ALIASES.items()
        if any(
            re.search(rf"\b{re.escape(alias)}\b", normalized)
            for alias in aliases
        )
    }
    # "jadwal" alone means date; "ubah jadwal ... jam" must resolve to time.
    if "time" in matches and "date" in matches and "jadwal" in normalized:
        matches.discard("date")
    return matches.pop() if len(matches) == 1 else None
