import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.chat import agent as chat_agent
from app.core.config import settings
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.security import create_customer_access_token
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

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


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
    HANDOFF_SCENARIOS = _load_scenarios("future_handoff.json")

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
        self.general_ai = AsyncMock(return_value="Bantuan umum tersedia.")
        self.general_reply_patch = patch.object(
            chat_agent.ai,
            "chat",
            self.general_ai,
        )
        self.classifier_reply_patch = patch.object(
            chat_agent.intent_classifier.ai,
            "chat",
            AsyncMock(return_value='{"intent": "general", "confidence": 0.0}'),
        )
        self.extractor_patch.start()
        self.general_reply_patch.start()
        self.classifier_reply_patch.start()

    def tearDown(self):
        self.extractor_patch.stop()
        self.general_reply_patch.stop()
        self.classifier_reply_patch.stop()
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

    def test_phase_b_handoff_scenarios(self):
        for scenario in self.HANDOFF_SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                if scenario.get("simulate_internal_error"):
                    context_manager = patch.object(
                        self.service,
                        "list_recent_reservations",
                        side_effect=RuntimeError("simulated service failure"),
                    )
                elif scenario.get("simulate_low_confidence"):
                    context_manager = patch.object(
                        chat_agent.intent_classifier,
                        "classify",
                        AsyncMock(return_value={"intent": "ambiguous", "confidence": 0.1}),
                    )
                else:
                    context_manager = nullcontext()

                with context_manager:
                    result = self.simulator.run(scenario)

                message = scenario["customer_messages"][-1]
                turn = len(scenario["customer_messages"])
                context = f"scenario={scenario['name']} turn={turn} message={message!r}"
                handoff_state = result.state.get("handoff_state") or {}
                self.assertEqual(result.errors, [], f"{context}; errors={result.errors}")
                self.assertEqual(result.intents[-1], scenario["expected_intent"], context)
                for expected_reply in scenario["expected_reply_contains"]:
                    self.assertIn(expected_reply, result.replies[-1], context)
                self.assertTrue(result.handoff, context)
                self.assertEqual(result.ticket_category, scenario["expected_ticket_category"], context)
                self.assertEqual(handoff_state.get("category"), scenario["expected_state"]["category"], context)
                self.assertTrue(handoff_state.get("created_at"), context)

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

    def test_handoff_locks_automation_but_other_customer_is_unaffected(self):
        session_id = "handoff-lock-session"
        token_a, _ = create_customer_access_token(self.customer_a.id, self.customer_a.token_version)
        token_b, _ = create_customer_access_token(self.customer_b.id, self.customer_b.token_version)
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        initial = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "saya ingin bicara dengan Rizal"},
            headers=headers_a,
        )
        locked = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "ubah reservasi saya"},
            headers=headers_a,
        )
        status = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "status handoff"},
            headers=headers_a,
        )
        unaffected = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "lihat reservasi saya"},
            headers=headers_b,
        )

        self.assertIn("meneruskan percakapan", initial.json()["reply"])
        self.assertIn("menunggu bantuan petugas", locked.json()["reply"])
        self.assertIn("menunggu bantuan petugas", status.json()["reply"])
        self.assertIn("Customer B", unaffected.json()["reply"])
        state_a = chat_agent.memory_manager.get_session(
            build_authenticated_memory_key(self.customer_a.id, session_id),
        )
        self.assertTrue(state_a["handoff_required"])
        self.assertNotIn("notifikasi", initial.json()["reply"].lower())

    def test_explicit_human_handoff_phrases_bypass_general_ai(self):
        phrases = (
            "hubungkan saya ke Rizal",
            "saya ingin bicara dengan Rizal",
            "saya mau bicara dengan owner",
            "hubungkan ke admin",
            "panggil petugas",
            "saya ingin bicara dengan manusia",
            "hubungkan ke customer service",
            "Hubungkan Saya ke RIZAL",
        )
        token, _ = create_customer_access_token(
            self.customer_a.id,
            self.customer_a.token_version,
        )
        headers = {"Authorization": f"Bearer {token}"}

        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                session_id = f"explicit-handoff-{index}"
                memory_key = build_authenticated_memory_key(
                    self.customer_a.id,
                    session_id,
                )
                chat_agent.memory_manager.clear_session(memory_key)
                response = self.client.post(
                    "/chat",
                    json={"session_id": session_id, "message": phrase},
                    headers=headers,
                )
                state = chat_agent.memory_manager.get_session(memory_key)
                self.assertEqual(response.status_code, 200)
                self.assertIn("meneruskan percakapan", response.json()["reply"])
                self.assertTrue(state["handoff_required"])
                self.assertEqual(
                    state["handoff_state"]["category"],
                    "explicit_human_request",
                )

        self.general_ai.assert_not_awaited()

    def test_handoff_counters_reset_and_remain_scoped_to_workflow_stage(self):
        reset_scenario = {
            "authenticated_customer": "customer_a",
            "session_id": "handoff-reset",
            "customer_messages": ["zxqv", "lihat reservasi saya", "qwer"],
        }
        reset_result = self.simulator.run(reset_scenario)
        self.assertFalse(reset_result.handoff)
        self.assertEqual(reset_result.state.get("misunderstanding_count"), 1)

        invalid_scope_scenario = {
            "authenticated_customer": "customer_a",
            "session_id": "handoff-invalid-scope",
            "customer_messages": ["ubah reservasi saya", "abc", "abc", "1", "bukan field"],
        }
        invalid_result = self.simulator.run(invalid_scope_scenario)
        self.assertFalse(invalid_result.handoff)
        self.assertEqual(invalid_result.state.get("invalid_input_count"), 1)

    def test_ambiguous_action_requires_clarification_then_handoff(self):
        scenario = {
            "authenticated_customer": "customer_a",
            "session_id": "handoff-ambiguous",
            "customer_messages": [
                "ubah atau batalkan reservasi saya",
                "ubah atau batalkan reservasi saya",
            ],
        }
        result = self.simulator.run(scenario)
        self.assertTrue(result.handoff)
        self.assertEqual(result.ticket_category, "ambiguous_intent")
        self.assertIn("perlu diteruskan kepada petugas", result.replies[-1])

    def test_repeated_misunderstanding_is_scoped_and_resets_after_valid_intent(self):
        session_id = "misunderstanding-shared-session"

        def send(customer, message):
            token, _ = create_customer_access_token(customer.id, customer.token_version)
            return self.client.post(
                "/chat",
                json={"session_id": session_id, "message": message},
                headers={"Authorization": f"Bearer {token}"},
            )

        first_unclear = send(self.customer_a, "asdasdasd")
        customer_b_unclear = send(self.customer_b, "asdasdasd")
        second_unclear = send(self.customer_a, "zxqv qwerty tidak jelas")
        key_a = build_authenticated_memory_key(self.customer_a.id, session_id)
        key_b = build_authenticated_memory_key(self.customer_b.id, session_id)

        self.assertIn("belum memahami", first_unclear.json()["reply"])
        self.assertIn("belum memahami", customer_b_unclear.json()["reply"])
        self.assertIn("perlu diteruskan kepada petugas", second_unclear.json()["reply"])
        self.assertTrue(chat_agent.memory_manager.get_session(key_a)["handoff_required"])
        self.assertFalse(chat_agent.memory_manager.get_session(key_b).get("handoff_required"))
        self.assertEqual(
            chat_agent.memory_manager.get_session(key_b)["misunderstanding_count"],
            1,
        )
        self.general_ai.assert_not_awaited()

        reset_session = "misunderstanding-reset-session"

        def send_reset(message):
            token, _ = create_customer_access_token(
                self.customer_a.id,
                self.customer_a.token_version,
            )
            return self.client.post(
                "/chat",
                json={"session_id": reset_session, "message": message},
                headers={"Authorization": f"Bearer {token}"},
            )

        send_reset("asdasdasd")
        valid = send_reset("lihat reservasi saya")
        after_reset = send_reset("zxqv qwerty tidak jelas")
        reset_key = build_authenticated_memory_key(self.customer_a.id, reset_session)
        reset_state = chat_agent.memory_manager.get_session(reset_key)
        self.assertIn("Customer A", valid.json()["reply"])
        self.assertIn("belum memahami", after_reset.json()["reply"])
        self.assertFalse(reset_state.get("handoff_required"))
        self.assertEqual(reset_state["misunderstanding_count"], 1)

    def test_informational_question_does_not_increment_misunderstanding(self):
        scenario = {
            "authenticated_customer": "customer_a",
            "session_id": "misunderstanding-information",
            "customer_messages": ["bagaimana cara mengubah reservasi?"],
        }
        result = self.simulator.run(scenario)
        self.assertFalse(result.handoff)
        self.assertEqual(result.state.get("misunderstanding_count"), 0)


if __name__ == "__main__":
    unittest.main()
