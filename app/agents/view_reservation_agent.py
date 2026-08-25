from typing import Any

from sqlalchemy.orm import Session

from app.core.ownership import MissingOwnerCustomerError, require_owner_customer_id
from app.core.locale import format_reservation, tr
from app.services.reservation.service import ReservationService
from app.services.reservation.public_reference import (
    PublicReservationReferenceUnavailableError,
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
                "response": tr("authorization_required"),
            }
        except PublicReservationReferenceUnavailableError:
            return {
                "status": "reference_unavailable",
                "response": tr("reference_unavailable_view"),
            }
        recent_reservations = reservations[:5]

        if not recent_reservations:
            return {
                "status": "viewed",
                "response": tr("no_reservations"),
            }

        records = "\n\n".join(
            self._format_reservation(reservation)
            for reservation in recent_reservations
        )
        return {
            "status": "viewed",
            "response": tr("reservation_list", records=records),
        }

    def _format_reservation(self, reservation: Any) -> str:
        return format_reservation(reservation)
