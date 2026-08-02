from typing import Any

from sqlalchemy.orm import Session

from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.services.reservation.service import ReservationService
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
)


VIEW_REFERENCE_UNAVAILABLE_RESPONSE = (
    "Data reservasi belum dapat ditampilkan dengan aman. Silakan coba lagi nanti."
)


class ViewReservationAgent:
    """Read and format the latest reservation records."""

    def __init__(self, reservation_service: ReservationService | None = None):
        self.reservation_service = reservation_service or ReservationService()

    async def run(
        self,
        db: Session,
        session_id: str,
        owner_customer_id,
    ) -> dict[str, Any]:
        try:
            require_owner_customer_id(owner_customer_id)
            reservations = self.reservation_service.list_recent_reservations(
                db,
                owner_customer_id=owner_customer_id,
                limit=5,
            )
        except MissingOwnerCustomerError:
            return {
                "status": "authorization_required",
                "response": "Identitas pelanggan tidak valid atau telah kedaluwarsa.",
            }
        except PublicReservationReferenceUnavailableError:
            return {
                "status": "reference_unavailable",
                "response": VIEW_REFERENCE_UNAVAILABLE_RESPONSE,
            }
        recent_reservations = reservations[:5]

        if not recent_reservations:
            return {
                "status": "viewed",
                "response": "Belum ada reservasi.",
            }

        records = "\n\n".join(
            self._format_reservation(reservation)
            for reservation in recent_reservations
        )
        return {
            "status": "viewed",
            "response": f"Daftar reservasi terbaru:\n\n{records}",
        }

    def _format_reservation(self, reservation: Any) -> str:
        return (
            f"Referensi reservasi: {reservation.reference}\n"
            f"Nama: {reservation.name}\n"
            f"Jumlah Orang: {reservation.people}\n"
            f"Tanggal: {reservation.date}\n"
            f"Jam: {reservation.time}\n"
            f"Status: {reservation.status}"
        )
