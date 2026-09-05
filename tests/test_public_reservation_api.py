"""Reference-only public reservation API contract tests."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import get_current_customer
from app.core.customer_identity import AuthenticatedCustomer
from app.core.transaction_errors import PersistenceOperationError
from app.db.database import get_db
from app.main import create_app
from app.services.reservation.dto import PersistedReservationDTO
from app.services.reservation.errors import (
    PastReservationDateError,
    PastReservationTimeError,
)


REFERENCE = "RSV_12121212121212121212121212121212"
OTHER_REFERENCE = "RSV_34343434343434343434343434343434"
SEEDED_DATABASE_ID = (2**30) + 73_991


def persisted(
    *,
    reference=REFERENCE,
    status="pending",
    identifier=SEEDED_DATABASE_ID,
):
    return PersistedReservationDTO(
        id=identifier,
        name="Rizal",
        people=2,
        date="2026-08-10",
        time="19:00",
        status=status,
        reference=reference,
    )


class PublicReservationAPITests(unittest.TestCase):
    def setUp(self):
        self.owner_id = uuid4()
        self.db = object()
        self.app = create_app(
            SimpleNamespace(APP_ENV="test", APP_NAME="AURA", VERSION="test")
        )
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_customer] = lambda: (
            AuthenticatedCustomer(
                id=self.owner_id,
                token_version=1,
                is_active=True,
            )
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()

    @staticmethod
    def create_body():
        return {
            "name": "Rizal",
            "people": 2,
            "date": "2026-08-10",
            "time": "19:00",
        }

    def assert_reference_only(self, response):
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["reference"], REFERENCE)
        self.assertNotIn("id", payload)
        self.assertNotIn("reservationId", payload)
        self.assertNotIn(str(SEEDED_DATABASE_ID), response.text)

    def test_create_is_reference_only_and_owner_comes_from_authentication(self):
        with patch(
            "app.api.reservation.service.create_reservation",
            return_value=persisted(),
        ) as create:
            response = self.client.post(
                "/reservation/",
                json=self.create_body(),
                headers={"X-Session-ID": "ignored"},
            )
        self.assert_reference_only(response)
        self.assertEqual(
            create.call_args.kwargs["owner_customer_id"],
            self.owner_id,
        )

    def test_create_past_date_uses_safe_domain_error(self):
        with patch(
            "app.api.reservation.service.create_reservation",
            side_effect=PastReservationDateError(),
        ):
            response = self.client.post(
                "/reservation/",
                json=self.create_body(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {
                "code": "PAST_RESERVATION_DATE",
                "detail": (
                    "That reservation date has already passed. Please choose "
                    "today or a future date."
                ),
            },
        )

    def test_create_past_time_uses_safe_domain_error(self):
        with patch(
            "app.api.reservation.service.create_reservation",
            side_effect=PastReservationTimeError(),
        ):
            response = self.client.post(
                "/reservation/",
                json=self.create_body(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {
                "code": "PAST_RESERVATION_TIME",
                "detail": (
                    "That reservation time has already passed. Please choose "
                    "a later time."
                ),
            },
        )

    def test_list_is_reference_only_fixed_limit_and_total_counted(self):
        values = (persisted(), persisted(reference=OTHER_REFERENCE))
        with patch(
            "app.api.reservation.service.list_owner_reservations",
            return_value=(values, 2),
        ) as list_owner:
            response = self.client.get("/reservation/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(
            [item["reference"] for item in response.json()["reservations"]],
            [REFERENCE, OTHER_REFERENCE],
        )
        self.assertNotIn(str(SEEDED_DATABASE_ID), response.text)
        self.assertEqual(list_owner.call_args.kwargs["limit"], 50)
        self.assertEqual(
            list_owner.call_args.kwargs["owner_customer_id"],
            self.owner_id,
        )

    def test_detail_canonicalizes_mixed_case_before_service_call(self):
        mixed = REFERENCE[:4] + REFERENCE[4:].upper()
        with patch(
            "app.api.reservation.service.get_reservation_by_reference",
            return_value=persisted(),
        ) as get:
            response = self.client.get(f"/reservation/{mixed}")
        self.assert_reference_only(response)
        self.assertEqual(get.call_args.args[1], REFERENCE)
        self.assertEqual(
            get.call_args.kwargs["owner_customer_id"],
            self.owner_id,
        )

    def test_malformed_and_numeric_references_fail_before_service(self):
        for reference in ("malformed", str(SEEDED_DATABASE_ID)):
            with self.subTest(reference_kind=len(reference)):
                with patch(
                    "app.api.reservation.service.get_reservation_by_reference"
                ) as get:
                    response = self.client.get(f"/reservation/{reference}")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["code"],
                    "INVALID_RESERVATION_REFERENCE",
                )
                get.assert_not_called()
                self.assertNotIn(reference, response.text)

    def test_missing_and_cross_owner_are_identical(self):
        bodies = []
        for _case in ("missing", "cross-owner"):
            with patch(
                "app.api.reservation.service.get_reservation_by_reference",
                return_value=None,
            ):
                response = self.client.get(f"/reservation/{REFERENCE}")
            self.assertEqual(response.status_code, 404)
            bodies.append(response.json())
        self.assertEqual(bodies[0], bodies[1])
        self.assertNotIn(REFERENCE, str(bodies[0]))

    def test_update_accepts_exactly_one_business_field(self):
        with patch(
            "app.api.reservation.service.update_reservation_field_by_reference",
            return_value=persisted(),
        ) as update:
            response = self.client.patch(
                f"/reservation/{REFERENCE}",
                json={"people": 3},
            )
        self.assert_reference_only(response)
        self.assertEqual(update.call_args.args[1:4], (REFERENCE, "people", 3))

        invalid_bodies = (
            {},
            {"name": "Rizal", "people": 3},
            {"name": None},
            {"people": None},
            {"people": True},
            {"people": False},
            {"partySize": 3},
            {"people": 3, "partySize": 4},
            {"reservation_reference": REFERENCE},
            {"unknownAlias": "sensitive-input-marker"},
            {"reference": REFERENCE},
            {"id": SEEDED_DATABASE_ID},
            {"reservationId": str(SEEDED_DATABASE_ID)},
            {"status": "cancelled"},
            {"owner_customer_id": str(self.owner_id)},
        )
        for body in invalid_bodies:
            with self.subTest(fields=tuple(body)):
                with patch(
                    "app.api.reservation.service."
                    "update_reservation_field_by_reference"
                ) as update_invalid:
                    invalid = self.client.patch(
                        f"/reservation/{REFERENCE}",
                        json=body,
                    )
                self.assertEqual(invalid.status_code, 422)
                update_invalid.assert_not_called()
                self.assertNotIn("sensitive-input-marker", invalid.text)
                self.assertNotIn(REFERENCE, invalid.text)

    def test_cancel_is_state_transition_empty_body_and_hides_cancelled_oracle(self):
        with patch(
            "app.api.reservation.service.cancel_reservation_by_reference",
            return_value=persisted(status="cancelled"),
        ):
            response = self.client.post(f"/reservation/{REFERENCE}/cancel")
        self.assert_reference_only(response)
        self.assertEqual(response.json()["status"], "cancelled")

        with patch(
            "app.api.reservation.service.cancel_reservation_by_reference",
            return_value=None,
        ):
            unavailable = self.client.post(
                f"/reservation/{REFERENCE}/cancel"
            )
        self.assertEqual(unavailable.status_code, 404)

        with patch(
            "app.api.reservation.service.cancel_reservation_by_reference"
        ) as cancel:
            invalid = self.client.post(
                f"/reservation/{REFERENCE}/cancel",
                json={"confirm": True},
            )
        self.assertEqual(invalid.status_code, 422)
        cancel.assert_not_called()

    def test_unsafe_stored_reference_fails_create_detail_and_whole_list(self):
        unsafe_values = (None, "RSV_invalid", "RSV_" + ("A" * 32))
        for unsafe in unsafe_values:
            with self.subTest(kind=type(unsafe).__name__):
                with patch(
                    "app.api.reservation.service.create_reservation",
                    return_value=persisted(reference=unsafe),
                ):
                    create = self.client.post(
                        "/reservation/",
                        json=self.create_body(),
                    )
                self.assertEqual(create.status_code, 503)
                self.assertNotIn(str(unsafe), create.text)

                with patch(
                    "app.api.reservation.service.list_owner_reservations",
                    return_value=((persisted(), persisted(reference=unsafe)), 2),
                ):
                    listed = self.client.get("/reservation/")
                self.assertEqual(listed.status_code, 503)
                self.assertNotIn("reservations", listed.json())

    def test_persistence_error_uses_existing_safe_envelope(self):
        with patch(
            "app.api.reservation.service.get_reservation_by_reference",
            side_effect=PersistenceOperationError(),
        ):
            response = self.client.get(f"/reservation/{REFERENCE}")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "PERSISTENCE_OPERATION_FAILED")
        self.assertNotIn(REFERENCE, response.text)


if __name__ == "__main__":
    unittest.main()
