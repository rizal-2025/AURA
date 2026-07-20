from typing import Any

from sqlalchemy.orm import Session

from app.brain.memory_manager import MemoryManager
from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.services.reservation.service import ReservationService


class CancelReservationAgent:
    """Guide a user through cancelling an existing reservation."""

    SELECT_RESERVATION_ID = "select_reservation_id"
    CONFIRM_CANCELLATION = "confirm_cancellation"

    POSITIVE_CONFIRMATION_ANSWERS = {
        "ya",
        "iya",
        "yes",
        "benar",
        "betul",
        "oke",
        "ok",
        "okay",
    }
    NEGATIVE_CONFIRMATION_ANSWERS = {
        "tidak",
        "bukan",
        "salah",
        "no",
        "nope",
        "nggak",
        "gak",
    }

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        reservation_service: ReservationService | None = None,
    ):
        self.memory_manager = memory_manager or MemoryManager()
        self.reservation_service = reservation_service or ReservationService()

    async def run(
        self,
        db: Session,
        session_id: str,
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        try:
            require_owner_customer_id(owner_customer_id)
        except MissingOwnerCustomerError:
            return {
                "status": "authorization_required",
                "response": "Identitas pelanggan tidak valid atau telah kedaluwarsa.",
            }

        session = self.memory_manager.get_session(session_id)
        stage = session.get("cancel_reservation_stage")

        if stage is None:
            return self._start_cancellation(db, session, owner_customer_id)

        if stage == self.SELECT_RESERVATION_ID:
            return self._select_reservation(db, session, user_message, owner_customer_id)

        if stage == self.CONFIRM_CANCELLATION:
            return self._confirm_cancellation(db, session, user_message, owner_customer_id)

        self._clear_cancellation_state(session)
        return self._start_cancellation(db, session, owner_customer_id)

    def _start_cancellation(
        self,
        db: Session,
        session: dict[str, Any],
        owner_customer_id,
    ) -> dict[str, Any]:
        reservations = self.reservation_service.list_recent_reservations(
            db,
            owner_customer_id=owner_customer_id,
            limit=5,
        )
        recent_reservations = reservations[:5]

        self._clear_cancellation_state(session)
        if not recent_reservations:
            return {
                "status": "awaiting_cancellation",
                "response": "Belum ada reservasi yang dapat dibatalkan.",
            }

        session["cancel_reservation_stage"] = self.SELECT_RESERVATION_ID
        records = "\n\n".join(
            self._format_reservation(reservation)
            for reservation in recent_reservations
        )
        return {
            "status": "awaiting_cancellation",
            "response": (
                f"Daftar reservasi terbaru:\n\n{records}\n\n"
                "Pilih ID reservasi yang ingin dibatalkan."
            ),
        }

    def _select_reservation(
        self,
        db: Session,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        reservation_id = self._parse_reservation_id(user_message)
        if reservation_id is None:
            return {
                "status": "awaiting_cancellation",
                "response": "Masukkan ID reservasi yang valid.",
                "invalid_input": True,
            }

        reservation = self.reservation_service.get_reservation_by_id(
            db,
            reservation_id,
            owner_customer_id=owner_customer_id,
        )
        if reservation is None:
            return {
                "status": "awaiting_cancellation",
                "response": "ID reservasi tidak ditemukan. Pilih ID yang tersedia.",
                "invalid_input": True,
            }

        if self._is_cancelled(reservation):
            session["cancel_reservation_id"] = None
            return {
                "status": "awaiting_cancellation",
                "response": "Reservasi ini sudah dibatalkan. Pilih ID reservasi lain.",
            }

        session.update(
            {
                "cancel_reservation_id": reservation_id,
                "cancel_reservation_stage": self.CONFIRM_CANCELLATION,
            }
        )
        return {
            "status": "awaiting_cancellation",
            "response": (
                f"Reservasi dipilih:\n\n{self._format_reservation(reservation)}\n\n"
                "Yakin ingin membatalkan reservasi ini? Ya / Tidak"
            ),
        }

    def _confirm_cancellation(
        self,
        db: Session,
        session: dict[str, Any],
        user_message: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        reservation_id = session.get("cancel_reservation_id")
        if not isinstance(reservation_id, int):
            self._clear_cancellation_state(session)
            return {
                "status": "awaiting_cancellation",
                "response": "Sesi pembatalan tidak valid. Mulai lagi dengan 'batalkan reservasi saya'.",
            }

        normalized_message = " ".join(user_message.lower().strip().split())
        if normalized_message in self.NEGATIVE_CONFIRMATION_ANSWERS:
            self._clear_cancellation_state(session)
            return {
                "status": "cancellation_rejected",
                "response": "Pembatalan reservasi dibatalkan. Tidak ada perubahan pada reservasi.",
            }

        if normalized_message not in self.POSITIVE_CONFIRMATION_ANSWERS:
            return {
                "status": "awaiting_cancellation",
                "response": "Yakin ingin membatalkan reservasi ini? Ya / Tidak",
            }

        cancelled_reservation = self.reservation_service.cancel_reservation(
            db,
            reservation_id,
            owner_customer_id=owner_customer_id,
        )
        if cancelled_reservation is None:
            current_reservation = self.reservation_service.get_reservation_by_id(
                db,
                reservation_id,
                owner_customer_id=owner_customer_id,
            )
            self._clear_cancellation_state(session)
            if current_reservation is not None and self._is_cancelled(current_reservation):
                response = "Reservasi ini sudah dibatalkan. Tidak ada perubahan tambahan."
            else:
                response = "ID reservasi tidak ditemukan. Tidak ada perubahan pada reservasi."
            return {
                "status": "awaiting_cancellation",
                "response": response,
            }

        self._clear_cancellation_state(session)
        return {
            "status": "cancelled",
            "response": (
                "Reservasi berhasil dibatalkan:\n\n"
                f"{self._format_reservation(cancelled_reservation)}"
            ),
        }

    def _clear_cancellation_state(self, session: dict[str, Any]) -> None:
        session["cancel_reservation_stage"] = None
        session["cancel_reservation_id"] = None

    def _parse_reservation_id(self, user_message: str) -> int | None:
        text = user_message.strip()
        return int(text) if text.isdigit() else None

    def _is_cancelled(self, reservation: Any) -> bool:
        return str(getattr(reservation, "status", "")).lower() == "cancelled"

    def _format_reservation(self, reservation: Any) -> str:
        return (
            f"ID: {reservation.id}\n"
            f"Nama: {reservation.name}\n"
            f"Jumlah Orang: {reservation.people}\n"
            f"Tanggal: {reservation.date}\n"
            f"Jam: {reservation.time}\n"
            f"Status: {reservation.status}"
        )
