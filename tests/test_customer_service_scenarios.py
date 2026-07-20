import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.chat import agent as chat_agent
from app.core.config import settings
from app.core.conversation_memory import build_authenticated_memory_key
from app.db.database import get_db
from app.main import app
from tests.customer_service.simulator import ConversationSimulator


SCENARIO_DIRECTORY = Path(__file__).parent / "customer_service" / "scenarios"


def _load_scenarios(filename: str) -> list[dict]:
    with (SCENARIO_DIRECTORY / filename).open(encoding="utf-8") as scenario_file:
        return json.load(scenario_file)


class FakeCustomerDB:
    def __init__(self, customers):
        self.customers = customers

    def get(self, _model, customer_id):
        return self.customers.get(customer_id)


class ScenarioReservationService:
    """Small in-memory read model for endpoint conversation scenarios."""

    def __init__(self, customer_a_id, customer_b_id):
        self.reservations = {
            1: SimpleNamespace(
                id=1, name="Customer A", people=4, date="2026-08-01",
                time="19:00", status="pending", owner_customer_id=customer_a_id,
            ),
            2: SimpleNamespace(
                id=2, name="Customer B", people=2, date="2026-08-02",
                time="20:00", status="pending", owner_customer_id=customer_b_id,
            ),
        }

    def list_recent_reservations(self, _db, owner_customer_id, limit=5):
        return [
            reservation
            for reservation in sorted(self.reservations.values(), key=lambda item: item.id, reverse=True)
            if reservation.owner_customer_id == owner_customer_id
        ][:limit]

    def get_reservation_by_id(self, _db, reservation_id, owner_customer_id):
        reservation = self.reservations.get(reservation_id)
        if reservation is None or reservation.owner_customer_id != owner_customer_id:
            return None
        return reservation


class TestCustomerServiceScenarios(unittest.TestCase):
    CURRENT_SCENARIOS = _load_scenarios("reservation_intents.json")

    def setUp(self):
        self.original_secret = settings.AUTH_JWT_SECRET
        self.original_issuer = settings.AUTH_JWT_ISSUER
        self.original_audience = settings.AUTH_JWT_AUDIENCE
        settings.AUTH_JWT_SECRET = "customer-service-test-secret-0123456789"
        settings.AUTH_JWT_ISSUER = "aura-customer-service-tests"
        settings.AUTH_JWT_AUDIENCE = "aura-customer-service-api"

        self.customer_a = SimpleNamespace(id=uuid4(), is_active=True, token_version=1)
        self.customer_b = SimpleNamespace(id=uuid4(), is_active=True, token_version=1)
        self.customers = {"customer_a": self.customer_a, "customer_b": self.customer_b}
        self.database = FakeCustomerDB({
            self.customer_a.id: self.customer_a,
            self.customer_b.id: self.customer_b,
        })
        self.service = ScenarioReservationService(self.customer_a.id, self.customer_b.id)

        def override_get_db():
            yield self.database

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.simulator = ConversationSimulator(self.client, self.customers)

        self.original_view_service = chat_agent.view_reservation_agent.reservation_service
        self.original_update_service = chat_agent.update_reservation_agent.reservation_service
        self.original_cancel_service = chat_agent.cancel_reservation_agent.reservation_service
        chat_agent.view_reservation_agent.reservation_service = self.service
        chat_agent.update_reservation_agent.reservation_service = self.service
        chat_agent.cancel_reservation_agent.reservation_service = self.service

        reservation_agent = chat_agent.workflow._agents["reservation"]
        self.extractor_patch = patch.object(
            reservation_agent.entity_extractor,
            "extract",
            AsyncMock(return_value={}),
        )
        self.general_reply_patch = patch.object(
            chat_agent.ai,
            "chat",
            AsyncMock(return_value="Bantuan umum tersedia."),
        )
        self.extractor_patch.start()
        self.general_reply_patch.start()

    def tearDown(self):
        self.extractor_patch.stop()
        self.general_reply_patch.stop()
        app.dependency_overrides.clear()
        chat_agent.view_reservation_agent.reservation_service = self.original_view_service
        chat_agent.update_reservation_agent.reservation_service = self.original_update_service
        chat_agent.cancel_reservation_agent.reservation_service = self.original_cancel_service
        settings.AUTH_JWT_SECRET = self.original_secret
        settings.AUTH_JWT_ISSUER = self.original_issuer
        settings.AUTH_JWT_AUDIENCE = self.original_audience

    def test_current_customer_service_scenarios(self):
        for scenario in self.CURRENT_SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                result = self.simulator.run(scenario)
                message = scenario["customer_messages"][-1]
                turn = len(scenario["customer_messages"])
                context = f"scenario={scenario['name']} turn={turn} message={message!r}"

                self.assertEqual(result.errors, [], f"{context}; errors={result.errors}")
                self.assertEqual(
                    result.intents[-1], scenario["expected_intent"],
                    f"{context}; expected intent={scenario['expected_intent']!r}, actual={result.intents[-1]!r}",
                )
                for expected_reply in scenario["expected_reply_contains"]:
                    self.assertIn(
                        expected_reply, result.replies[-1],
                        f"{context}; expected reply fragment={expected_reply!r}, actual={result.replies[-1]!r}",
                    )
                for field, expected_value in scenario["expected_state"].items():
                    self.assertEqual(
                        result.state.get(field), expected_value,
                        f"{context}; expected state {field}={expected_value!r}, actual={result.state.get(field)!r}",
                    )
                self.assertEqual(result.handoff, scenario["expected_handoff"], context)
                self.assertEqual(result.ticket_category, scenario["expected_ticket_category"], context)

    def test_identical_session_id_remains_isolated_between_customers(self):
        scenario_a = dict(self.CURRENT_SCENARIOS[0], session_id="shared-scenario-session")
        scenario_b = dict(self.CURRENT_SCENARIOS[0], authenticated_customer="customer_b", session_id="shared-scenario-session")
        result_a = self.simulator.run(scenario_a)
        result_b = self.simulator.run(scenario_b)

        key_a = build_authenticated_memory_key(self.customer_a.id, "shared-scenario-session")
        key_b = build_authenticated_memory_key(self.customer_b.id, "shared-scenario-session")
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(result_a.state["update_reservation_stage"], "select_reservation_id")
        self.assertEqual(result_b.state["update_reservation_stage"], "select_reservation_id")
        self.assertIn("Customer B", result_b.replies[-1])
        self.assertNotIn("shared-scenario-session", chat_agent.memory_manager._sessions)


def _add_future_handoff_skip_test(scenario: dict) -> None:
    reason = scenario["skip_reason"]

    @unittest.skip(reason)
    def test_scenario(self):
        self.fail(f"Handoff scenario unexpectedly executed: {scenario['name']}")

    test_scenario.__name__ = f"test_{scenario['name']}"
    setattr(TestCustomerServiceScenarios, test_scenario.__name__, test_scenario)


for _scenario in _load_scenarios("future_handoff.json"):
    _add_future_handoff_skip_test(_scenario)


if __name__ == "__main__":
    unittest.main()
