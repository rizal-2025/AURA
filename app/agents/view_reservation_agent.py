from typing import Any

from sqlalchemy.orm import Session

from app.services.reservation.service import ReservationService


class ViewReservationAgent:
    """Read and format the latest reservation records."""

    def __init__(self, reservation_service: ReservationService | None = None):
        self.reservation_service = reservation_service or ReservationService()

    async def run(
        self,
        db: Session,
        session_id: str,
    ) -> dict[str, Any]:
        reservations = self.reservation_service.list_recent_reservations(
            db,
            customer_id=session_id,
            limit=5,
        )
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
            f"ID: {reservation.id}\n"
            f"Nama: {reservation.name}\n"
            f"Jumlah Orang: {reservation.people}\n"
            f"Tanggal: {reservation.date}\n"
            f"Jam: {reservation.time}\n"
            f"Status: {reservation.status}"
        )
