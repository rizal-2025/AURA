"""Static OpenAPI and Pydantic schema checks for Phase-C contracts."""

from types import SimpleNamespace
import json
import unittest

from pydantic import ValidationError

from app.main import create_app
from app.schemas.demo_chat import DemoChatResponse, DemoReservationMutation
from app.schemas.demo_reservation_reset import DemoReservationItem
from app.schemas.reservation import PublicReservationResponse


REFERENCE = "RSV_56565656565656565656565656565656"
SEEDED_SCHEMA_PROOF_ID = (2**30) + 301_157


class PublicReservationOpenAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(
            SimpleNamespace(APP_ENV="test", APP_NAME="AURA", VERSION="test")
        )
        cls.schema = cls.app.openapi()

    def test_every_direct_operation_is_present_and_reference_only(self):
        paths = self.schema["paths"]
        self.assertEqual(
            set(paths["/reservation/"]),
            {"get", "post"},
        )
        self.assertEqual(
            set(paths["/reservation/{reference}"]),
            {"get", "patch"},
        )
        self.assertEqual(
            set(paths["/reservation/{reference}/cancel"]),
            {"post"},
        )
        for path, operations in paths.items():
            if not path.startswith("/reservation"):
                continue
            for operation in operations.values():
                for parameter in operation.get("parameters", []):
                    if parameter["name"] == "reference":
                        self.assertEqual(parameter["schema"]["type"], "string")

    def test_public_components_and_paths_have_no_numeric_reservation_identity(self):
        selected = {
            name: schema
            for name, schema in self.schema["components"]["schemas"].items()
            if name.startswith("PublicReservation")
            or name.startswith("ReservationCreate")
            or name.startswith("ReservationUpdate")
        }
        rendered = json.dumps(
            {
                "paths": {
                    path: value
                    for path, value in self.schema["paths"].items()
                    if path.startswith("/reservation")
                },
                "schemas": selected,
            },
            sort_keys=True,
        )
        self.assertNotIn('"reservationId"', rendered)
        self.assertNotIn(str(SEEDED_SCHEMA_PROOF_ID), rendered)
        for schema in selected.values():
            self.assertNotIn("id", schema.get("properties", {}))
        response = selected["PublicReservationResponse"]
        self.assertIn("reference", response["properties"])
        self.assertIn("opaque", response["properties"]["reference"]["description"].lower())

    def test_public_and_demo_models_reject_extra_fields(self):
        with self.assertRaises(ValidationError):
            PublicReservationResponse(
                reference=REFERENCE,
                name="Rizal",
                people=2,
                date="2026-08-10",
                time="19:00",
                status="pending",
                id=1,
            )
        with self.assertRaises(ValidationError):
            DemoReservationItem(
                reservation_reference=REFERENCE,
                status="pending",
                reservation_date="2026-08-10",
                reservation_time="19:00",
                party_size=2,
                id=1,
            )

    def test_internal_demo_serialization_aliases_are_reference_only(self):
        mutation_schema = DemoReservationMutation.model_json_schema(
            mode="serialization"
        )
        item_schema = DemoReservationItem.model_json_schema(mode="serialization")
        response_schema = DemoChatResponse.model_json_schema(mode="serialization")
        rendered = json.dumps(
            [mutation_schema, item_schema, response_schema],
            sort_keys=True,
        )
        self.assertIn('"operation"', rendered)
        self.assertIn('"reservationReference"', rendered)
        self.assertNotIn('"reservationId"', rendered)
        self.assertNotIn('"id"', json.dumps(mutation_schema, sort_keys=True))
        self.assertNotIn('"id"', json.dumps(item_schema, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
