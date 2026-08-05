"""Strict public mutation mapper and persistence codec tests."""

import unittest

from pydantic import ValidationError

from app.agents.result import (
    ReservationOperationResult,
    ReservationOperationType,
)
from app.schemas.demo_chat import DemoReservationMutation
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.demo_reservation_mutation import (
    decode_persisted_reservation_mutation,
    encode_reservation_operation,
)


REFERENCE = "RSV_" + ("b" * 32)


class DemoReservationMutationTests(unittest.TestCase):
    def test_exact_internal_to_public_mapping_and_round_trip(self):
        for internal, public in (
            (ReservationOperationType.CREATED, "created"),
            (ReservationOperationType.UPDATED, "updated"),
            (ReservationOperationType.CANCELLED, "cancelled"),
        ):
            with self.subTest(operation=public):
                encoded = encode_reservation_operation(
                    ReservationOperationResult(internal, REFERENCE)
                )
                decoded = decode_persisted_reservation_mutation(
                    encoded.operation,
                    encoded.reference,
                )
                self.assertEqual(
                    decoded.model_dump(by_alias=True),
                    {
                        "operation": public,
                        "reservationReference": REFERENCE,
                    },
                )

    def test_null_operation_is_a_null_pair_and_decodes_to_null(self):
        encoded = encode_reservation_operation(None)
        self.assertEqual((encoded.operation, encoded.reference), (None, None))
        self.assertIsNone(decode_persisted_reservation_mutation(None, None))

    def test_invalid_persisted_pair_enum_and_reference_fail_closed(self):
        unsafe_pairs = (
            (None, REFERENCE),
            ("created", None),
            ("unknown", REFERENCE),
            ("created", "RSV_" + ("B" * 32)),
            ("created", "not-a-reference"),
        )
        for operation, reference in unsafe_pairs:
            with self.subTest(operation=operation):
                with self.assertRaises(DemoChatServiceUnavailableError) as error:
                    decode_persisted_reservation_mutation(operation, reference)
                self.assertNotIn(str(reference), repr(error.exception))

    def test_mapper_rejects_non_internal_result_without_reading_reply(self):
        class ReplyLikeObject:
            reply = "created " + REFERENCE
            operation = ReservationOperationType.CREATED
            reference = REFERENCE

        with self.assertRaises(DemoChatServiceUnavailableError):
            encode_reservation_operation(ReplyLikeObject())

    def test_public_schema_is_frozen_exact_and_reference_only(self):
        mutation = DemoReservationMutation.model_validate(
            {
                "operation": "created",
                "reservation_reference": REFERENCE,
            }
        )
        with self.assertRaises(ValidationError):
            DemoReservationMutation.model_validate(
                {
                    "operation": "created",
                    "reservation_reference": REFERENCE,
                    "reservationId": 1,
                }
            )
        with self.assertRaises(ValidationError):
            mutation.operation = "updated"
        rendered = mutation.model_dump(by_alias=True)
        self.assertEqual(
            set(rendered),
            {"operation", "reservationReference"},
        )
        self.assertNotIn("id", rendered)


if __name__ == "__main__":
    unittest.main()
