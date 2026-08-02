import asyncio
import logging
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.workflow import AgentWorkflow
from app.brain.classifier import IntentClassifier
from app.brain.memory_manager import (
    CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS,
    CONVERSATION_SNAPSHOT_MAX_DEPTH,
    CONVERSATION_SNAPSHOT_MAX_TOTAL_NODES,
    ConversationSnapshot,
    MemoryManager,
)
from app.brain.reservation_memory import (
    COMMITTED_MEMORY_UNAVAILABLE,
    COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
    COMMITTED_OPERATION_STATE_UNAVAILABLE_RESPONSE,
    OUTCOME_UNKNOWN,
    RESERVATION_PERSISTENCE_STATE,
    RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE,
    SESSION_UNUSABLE,
    publish_create_success,
    publish_reservation_persistence_blocker,
)
from app.brain.planner import Planner
from app.api.error_handlers import transaction_exception_handler
from app.core.memory_errors import (
    ConversationMemoryError,
    ConversationMemoryValidationError,
    PostCommitMemoryPublicationError,
)
from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)
from app.db.models.reservation import Reservation
from app.integrations.telegram.handlers import (
    MEMORY_PUBLICATION_UNAVAILABLE_REPLY,
    TelegramCustomerHandlers,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.reservation.dto import PersistedReservationDTO


def persisted(identifier=71, *, people=4, status="pending"):
    return PersistedReservationDTO(
        id=identifier,
        name="Rizal",
        people=people,
        date="2026-08-01",
        time="19:00",
        status=status,
        reference="RSV_71717171717171717171717171717171",
    )


def seed_create(memory, key):
    memory.get_session(key).update(
        {
            "intent": "reservation",
            "name": "Rizal",
            "people": 4,
            "date": "2026-08-01",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
            "editing_field": None,
            "asked_fields": ["name", "people"],
            "handoff_state": {
                "handoff_required": False,
                "category": "safe-marker",
            },
        }
    )


def build_orchestrator(memory):
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.memory_manager = memory
    orchestrator.ai = SimpleNamespace(
        chat=AsyncMock(return_value="Jawaban informasi aman")
    )
    classifier_provider = SimpleNamespace(
        chat=AsyncMock(
            return_value='{"intent":"general","confidence":0.99}'
        )
    )
    orchestrator.intent_classifier = IntentClassifier(
        provider=classifier_provider
    )
    orchestrator.planner = Planner()
    orchestrator.workflow = AgentWorkflow(memory_manager=memory)
    orchestrator.view_reservation_agent = SimpleNamespace(
        run=AsyncMock(
            return_value={
                "status": "viewed",
                "response": "Daftar reservasi terbaru",
            }
        )
    )
    orchestrator.update_reservation_agent = UpdateReservationAgent(
        memory_manager=memory
    )
    orchestrator.cancel_reservation_agent = CancelReservationAgent(
        memory_manager=memory
    )
    orchestrator.handoff_service = MagicMock()
    orchestrator.handoff_service.is_required.return_value = False
    orchestrator.handoff_service.explicit_response.return_value = (
        "Percakapan akan diteruskan kepada petugas."
    )
    return orchestrator


class SnapshotTests(unittest.TestCase):
    def test_runtime_defaults_are_reference_only(self):
        state = MemoryManager().get_session("reference-defaults")

        self.assertIsNone(state["reservation_reference"])
        self.assertIsNone(state["cancel_reservation_reference"])
        self.assertNotIn("reservation_id", state)
        self.assertNotIn("cancel_reservation_id", state)

    def test_nested_values_are_isolated_in_both_directions(self):
        memory = MemoryManager()
        state = memory.get_session("one")
        state["asked_fields"] = ["name"]
        state["nested"] = {"items": [{"value": 1}]}

        snapshot = memory.snapshot_conversation("one")
        state["asked_fields"].append("people")
        state["nested"]["items"][0]["value"] = 9

        first = snapshot.materialize()
        self.assertEqual(first["asked_fields"], ["name"])
        self.assertEqual(first["nested"], {"items": [{"value": 1}]})

        first["asked_fields"].append("date")
        first["nested"]["items"][0]["value"] = 7
        second = snapshot.materialize()
        self.assertEqual(second["asked_fields"], ["name"])
        self.assertEqual(second["nested"], {"items": [{"value": 1}]})

    def test_snapshot_has_immutable_public_interface(self):
        memory = MemoryManager()
        snapshot = memory.snapshot_conversation("one")
        with self.assertRaises(AttributeError):
            snapshot.extra = "unsafe"

    def test_replace_is_fresh_and_replaces_instead_of_merging(self):
        memory = MemoryManager()
        memory.get_session("one").update(
            {"obsolete": True, "nested": {"items": ["old"]}}
        )
        replacement = {"intent": "greeting", "nested": {"items": ["new"]}}

        memory.replace_conversation("one", replacement)
        replacement["nested"]["items"].append("caller-change")
        state = memory.get_session("one")

        self.assertNotIn("obsolete", state)
        self.assertEqual(state["nested"]["items"], ["new"])

        state["nested"]["items"].append("live-change")
        self.assertEqual(replacement["nested"]["items"], ["new", "caller-change"])

    def test_snapshot_is_scoped_to_one_conversation(self):
        memory = MemoryManager()
        memory.get_session("one")["marker"] = "first"
        memory.get_session("two")["marker"] = "second"

        captured = memory.snapshot_conversation("one").materialize()

        self.assertEqual(captured["marker"], "first")
        self.assertNotIn("second", captured.values())

    def test_unsafe_values_are_rejected_with_stable_private_error(self):
        unsafe_values = (
            Session(),
            Reservation(),
            object(),
            uuid4(),
            iter([1]),
            RuntimeError("private reservation value"),
        )
        for unsafe in unsafe_values:
            with self.subTest(kind=type(unsafe).__name__):
                memory = MemoryManager()
                memory.get_session("private-memory-key")["unsafe"] = unsafe
                with self.assertRaises(ConversationMemoryError) as raised:
                    memory.snapshot_conversation("private-memory-key")
                rendered = f"{raised.exception!s} {raised.exception!r}"
                self.assertIsInstance(
                    raised.exception,
                    ConversationMemoryValidationError,
                )
                self.assertEqual(
                    str(raised.exception),
                    "CONVERSATION_MEMORY_UNAVAILABLE",
                )
                self.assertNotIn("private-memory-key", rendered)
                self.assertNotIn("private reservation value", rendered)

    def test_non_finite_float_and_sensitive_keys_are_rejected(self):
        for state in (
            {"value": float("inf")},
            {"value": float("nan")},
            {"access_token": "private-token"},
        ):
            with self.subTest(keys=tuple(state)):
                memory = MemoryManager()
                with self.assertRaises(ConversationMemoryError):
                    memory.replace_conversation("one", state)

    def test_memory_error_uses_safe_generic_http_envelope(self):
        response = asyncio.run(
            transaction_exception_handler(
                None,
                ConversationMemoryValidationError(),
            )
        )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 503)
        self.assertIn("CONVERSATION_MEMORY_UNAVAILABLE", body)
        for private_value in (
            "owner:create",
            "Rizal",
            "2026-08-01",
            "SELECT",
        ):
            self.assertNotIn(private_value, body)

    def test_cycles_are_rejected_with_safe_validation_error(self):
        direct_dictionary = {}
        direct_dictionary["self"] = direct_dictionary
        direct_list = []
        direct_list.append(direct_list)
        first = {}
        second = {"first": first}
        first["second"] = second

        for value in (direct_dictionary, direct_list, first):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ConversationMemoryValidationError):
                    ConversationSnapshot({"value": value})

    def test_depth_container_and_total_node_limits_are_bounded(self):
        self.assertEqual(CONVERSATION_SNAPSHOT_MAX_DEPTH, 16)
        self.assertEqual(CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS, 256)
        self.assertEqual(CONVERSATION_SNAPSHOT_MAX_TOTAL_NODES, 2048)
        too_deep = []
        current = too_deep
        for _ in range(CONVERSATION_SNAPSHOT_MAX_DEPTH + 1):
            child = []
            current.append(child)
            current = child

        oversized_list = [0] * (
            CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS + 1
        )
        oversized_dictionary = {
            f"field_{index}": index
            for index in range(
                CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS + 1
            )
        }
        excessive_nodes = {
            "branches": [
                [0] * CONVERSATION_SNAPSHOT_MAX_CONTAINER_ITEMS
                for _ in range(8)
            ]
        }

        for value in (
            too_deep,
            oversized_list,
            oversized_dictionary,
            excessive_nodes,
        ):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ConversationMemoryValidationError):
                    ConversationSnapshot({"value": value})

    def test_missing_snapshot_is_pure_and_empty_replace_uses_defaults(self):
        memory = MemoryManager()

        missing = memory.snapshot_conversation("missing").materialize()

        self.assertNotIn("missing", memory._sessions)
        self.assertEqual(
            missing,
            MemoryManager._default_conversation_state(),
        )

        memory.replace_conversation("existing", {})
        self.assertEqual(
            memory.get_session("existing"),
            MemoryManager._default_conversation_state(),
        )

    def test_current_production_memory_inventory_is_snapshot_safe(self):
        memory = MemoryManager()
        state = memory.get_session("inventory")
        state.update(
            {
                "intent": "reservation",
                "intent_confidence": 0.99,
                "awaiting_confirmation": True,
                "asked_fields": ["name", "people", "date", "time"],
                "reservation_reference": "RSV_12121212121212121212121212121212",
                "update_reservation_stage": "input_value",
                "cancel_reservation_reference": "RSV_12121212121212121212121212121212",
                "cancel_reservation_stage": "confirm_cancellation",
                "misunderstanding_count": 1,
                "invalid_input_context": None,
                "handoff_state": {
                    "handoff_required": False,
                    "category": "explicit_human_request",
                    "attempt_count": 1,
                    "ticket_id": 9,
                    "ticket_number": "CS-2026-000009",
                    "created_at": "2026-07-25T01:02:03+00:00",
                },
                RESERVATION_PERSISTENCE_STATE: {
                    "status": OUTCOME_UNKNOWN,
                    "operation": "create",
                },
            }
        )

        materialized = memory.snapshot_conversation("inventory").materialize()

        self.assertEqual(materialized, state)

    def test_detached_live_reference_cannot_overwrite_replacement(self):
        memory = MemoryManager()
        stale = memory.get_session("one")
        stale["marker"] = "old"

        memory.replace_conversation("one", {"marker": "current"})
        stale["marker"] = "detached-change"

        self.assertEqual(memory.get_session("one")["marker"], "current")

    def test_forbidden_keys_are_exact_normalized_and_legitimate_ids_work(self):
        legitimate = {
            "reservation_reference": "RSV_11111111111111111111111111111111",
            "ticket_id": 2,
            "token_version": 1,
            "owner_customer_id": "internal-safe-scalar",
            "session_id": "internal-memory-reference",
        }
        ConversationSnapshot(legitimate)

        for forbidden in ("TOKEN", "ＴＯＫＥＮ", "Authorization", "password"):
            with self.subTest(key=forbidden):
                with self.assertRaises(ConversationMemoryValidationError):
                    ConversationSnapshot({forbidden: "private"})

    def test_post_commit_error_http_mapping_is_distinct_and_subclass_safe(self):
        class FuturePostCommitError(PostCommitMemoryPublicationError):
            pass

        for error_type in (
            PostCommitMemoryPublicationError,
            FuturePostCommitError,
        ):
            response = asyncio.run(
                transaction_exception_handler(None, error_type())
            )
            body = response.body.decode("utf-8")
            self.assertEqual(response.status_code, 503)
            self.assertIn("COMMITTED_OPERATION_STATE_UNAVAILABLE", body)
            self.assertIn("may already be completed", body)
            self.assertNotIn("could not be saved", body)

    def test_create_success_publication_requires_detached_typed_dto(self):
        memory = MemoryManager()
        snapshot = memory.snapshot_conversation("typed")
        arbitrary = SimpleNamespace(
            id=1,
            name="Rizal",
            people=4,
            date="2026-08-01",
            time="19:00",
            status="pending",
            owner_customer_id="must-not-enter-memory",
        )

        with self.assertRaises(ConversationMemoryValidationError):
            publish_create_success(
                memory,
                "typed",
                snapshot,
                arbitrary,
            )

        publish_create_success(
            memory,
            "typed",
            snapshot,
            persisted(1),
        )
        self.assertNotIn(
            "owner_customer_id",
            memory.get_session("typed"),
        )


class CreatePublicationTests(unittest.TestCase):
    def setUp(self):
        self.key = "owner:create"
        self.owner = uuid4()
        self.memory = MemoryManager()
        seed_create(self.memory, self.key)
        self.agent = ReservationAgent(memory_manager=self.memory)

    def _confirm(self):
        return asyncio.run(
            self.agent.handle_confirmation(
                "Ya",
                self.key,
                owner_customer_id=self.owner,
                db=object(),
            )
        )

    def test_success_is_published_only_after_service_return(self):
        def create(*_args, **_kwargs):
            during = self.memory.get_session(self.key)
            self.assertFalse(during["completed"])
            self.assertTrue(during["awaiting_confirmation"])
            self.assertNotIn("reservation_id", during)
            return persisted(901)

        self.agent.reservation_service.create_reservation = MagicMock(side_effect=create)
        result = self._confirm()
        state = self.memory.get_session(self.key)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            state["reservation_reference"],
            "RSV_71717171717171717171717171717171",
        )
        self.assertTrue(state["completed"])
        self.assertFalse(state["awaiting_confirmation"])
        self.assertIsNone(state["editing_field"])
        self.assertEqual(state["handoff_state"]["category"], "safe-marker")

    def test_precommit_failure_restores_exact_nested_confirmation_snapshot(self):
        original = self.memory.snapshot_conversation(self.key).materialize()

        def fail(*_args, **_kwargs):
            current = self.memory.get_session(self.key)
            current["asked_fields"].append("date")
            current["handoff_state"]["category"] = "mutated"
            raise PersistenceOperationError()

        self.agent.reservation_service.create_reservation = MagicMock(side_effect=fail)
        with self.assertRaises(PersistenceOperationError):
            self._confirm()

        self.assertEqual(self.memory.get_session(self.key), original)

    def test_unknown_and_unusable_publish_non_actionable_blocker(self):
        cases = (
            (PersistenceOutcomeUnknownError(), OUTCOME_UNKNOWN),
            (TransactionSessionUnusableError(), SESSION_UNUSABLE),
        )
        for error, expected_status in cases:
            with self.subTest(status=expected_status):
                memory = MemoryManager()
                seed_create(memory, self.key)
                agent = ReservationAgent(memory_manager=memory)
                agent.reservation_service.create_reservation = MagicMock(
                    side_effect=error
                )
                with self.assertRaises(type(error)):
                    asyncio.run(
                        agent.handle_confirmation(
                            "Ya",
                            self.key,
                            owner_customer_id=self.owner,
                            db=object(),
                        )
                    )

                state = memory.get_session(self.key)
                self.assertFalse(state["awaiting_confirmation"])
                self.assertIsNone(state["editing_field"])
                self.assertEqual(
                    state[RESERVATION_PERSISTENCE_STATE],
                    {"status": expected_status, "operation": "create"},
                )
                blocker_text = repr(state[RESERVATION_PERSISTENCE_STATE])
                for private_value in ("Rizal", "2026-08-01", "19:00", str(self.owner)):
                    self.assertNotIn(private_value, blocker_text)

    def test_blocker_prevents_second_create_mutation(self):
        service = MagicMock(side_effect=PersistenceOutcomeUnknownError())
        self.agent.reservation_service.create_reservation = service
        with self.assertRaises(PersistenceOutcomeUnknownError):
            self._confirm()

        result = self._confirm()

        self.assertEqual(result["status"], "persistence_uncertain")
        self.assertEqual(
            result["response"],
            RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE,
        )
        self.assertEqual(service.call_count, 1)

    def test_blocker_prevents_all_reservation_routing_without_handoff(self):
        self.memory.get_session(self.key)[RESERVATION_PERSISTENCE_STATE] = {
            "status": OUTCOME_UNKNOWN,
            "operation": "create",
        }
        self.memory.get_session(self.key)["awaiting_confirmation"] = False
        self.memory.get_session(self.key)["editing_field"] = None
        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.memory_manager = self.memory
        orchestrator.handoff_service = MagicMock()
        orchestrator.handoff_service.is_required.return_value = False
        orchestrator.view_reservation_agent = SimpleNamespace(
            run=AsyncMock(
                return_value={
                    "status": "viewed",
                    "response": "Daftar reservasi terbaru",
                }
            )
        )

        for message in (
            "buatkan reservasi",
            "ubah reservasi saya",
            "batalkan reservasi saya",
        ):
            with self.subTest(message=message):
                result = asyncio.run(
                    orchestrator.handle(
                        self.key,
                        message,
                        object(),
                        self.owner,
                    )
                )
                self.assertEqual(
                    result,
                    RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE,
                )
        orchestrator.handoff_service.require_handoff.assert_not_called()

        view_result = asyncio.run(
            orchestrator.handle(
                self.key,
                "lihat reservasi saya",
                object(),
                self.owner,
            )
        )
        self.assertEqual(view_result, "Daftar reservasi terbaru")
        self.assertIn(
            RESERVATION_PERSISTENCE_STATE,
            self.memory.get_session(self.key),
        )

    def test_blocker_allows_safe_paths_and_survives_them(self):
        state = self.memory.get_session(self.key)
        state[RESERVATION_PERSISTENCE_STATE] = {
            "status": OUTCOME_UNKNOWN,
            "operation": "create",
        }
        state["awaiting_confirmation"] = False
        state["intent"] = None
        orchestrator = build_orchestrator(self.memory)

        explicit = asyncio.run(
            orchestrator.handle(
                self.key,
                "hubungkan saya ke petugas",
                object(),
                self.owner,
            )
        )
        self.assertIn("petugas", explicit)
        orchestrator.handoff_service.require_handoff.assert_called_once()

        greeting = asyncio.run(
            orchestrator.handle(
                self.key,
                "Halo",
                object(),
                self.owner,
            )
        )
        self.assertIn("Halo", greeting)

        informational = asyncio.run(
            orchestrator.handle(
                self.key,
                "jam buka berapa?",
                object(),
                self.owner,
            )
        )
        self.assertEqual(informational, "Jawaban informasi aman")

        viewed = asyncio.run(
            orchestrator.handle(
                self.key,
                "lihat reservasi saya",
                object(),
                self.owner,
            )
        )
        self.assertEqual(viewed, "Daftar reservasi terbaru")
        self.assertIn(
            RESERVATION_PERSISTENCE_STATE,
            self.memory.get_session(self.key),
        )

    def test_blocker_rejects_mixed_and_active_mutation_continuation(self):
        state = self.memory.get_session(self.key)
        state[RESERVATION_PERSISTENCE_STATE] = {
            "status": OUTCOME_UNKNOWN,
            "operation": "create",
        }
        orchestrator = build_orchestrator(self.memory)

        mixed = asyncio.run(
            orchestrator.handle(
                self.key,
                "Halo, buat reservasi baru",
                object(),
                self.owner,
            )
        )
        self.assertEqual(mixed, RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE)

        state["awaiting_confirmation"] = True
        continuation = asyncio.run(
            orchestrator.handle(
                self.key,
                "Ya",
                object(),
                self.owner,
            )
        )
        self.assertEqual(
            continuation,
            RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE,
        )

    def test_older_snapshot_cannot_remove_emergency_guard(self):
        older = self.memory.snapshot_conversation(self.key)
        publish_reservation_persistence_blocker(
            self.memory,
            self.key,
            older,
            status=OUTCOME_UNKNOWN,
            operation="create",
        )

        self.memory.replace_conversation(self.key, older)

        blocked = self._confirm()
        self.assertEqual(
            blocked["response"],
            RESERVATION_PERSISTENCE_UNCERTAIN_RESPONSE,
        )
        self.assertIsNotNone(
            self.memory.get_reservation_mutation_guard(self.key)
        )

        self.memory.clear_session(self.key)
        self.assertIsNone(
            self.memory.get_reservation_mutation_guard(self.key)
        )

    def test_response_failure_after_service_return_preserves_success_memory(self):
        self.agent.reservation_service.create_reservation = MagicMock(
            return_value=persisted(902)
        )
        with patch.object(
            self.agent,
            "_create_success_response",
            side_effect=RuntimeError("synthetic formatting failure"),
        ):
            result = self._confirm()

        state = self.memory.get_session(self.key)
        self.assertEqual(
            result["response"],
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        self.assertTrue(state["completed"])
        self.assertEqual(
            state["reservation_reference"],
            "RSV_71717171717171717171717171717171",
        )
        self.assertFalse(state["awaiting_confirmation"])

    def test_confirmed_commit_publication_failure_installs_emergency_guard(self):
        service = MagicMock(return_value=persisted(903))
        self.agent.reservation_service.create_reservation = service

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=RuntimeError("private memory value"),
        ):
            with self.assertRaises(
                PostCommitMemoryPublicationError
            ) as raised:
                self._confirm()

        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("private memory value", repr(raised.exception))
        guard = self.memory.get_reservation_mutation_guard(self.key)
        self.assertEqual(
            guard,
            {
                "status": COMMITTED_MEMORY_UNAVAILABLE,
                "operation": "create",
            },
        )
        second = self._confirm()
        self.assertEqual(
            second["response"],
            COMMITTED_OPERATION_STATE_UNAVAILABLE_RESPONSE,
        )
        self.assertEqual(service.call_count, 1)

    def test_partial_success_publication_is_still_fail_closed(self):
        service = MagicMock(return_value=persisted(904))
        self.agent.reservation_service.create_reservation = service
        original_replace = self.memory.replace_conversation
        calls = 0

        def publish_then_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            original_replace(*args, **kwargs)
            if calls == 1:
                raise ConversationMemoryValidationError()

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=publish_then_fail,
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                self._confirm()

        self.assertFalse(self.memory.get_session(self.key)["awaiting_confirmation"])
        self.assertEqual(
            self.memory.get_reservation_mutation_guard(self.key)["status"],
            COMMITTED_MEMORY_UNAVAILABLE,
        )
        self._confirm()
        self.assertEqual(service.call_count, 1)

    def test_formatter_failure_through_orchestrator_creates_no_handoff(self):
        orchestrator = build_orchestrator(self.memory)
        reservation_agent = orchestrator.workflow._agents["reservation"]
        reservation_agent.reservation_service.create_reservation = MagicMock(
            return_value=persisted(905)
        )

        with patch.object(
            reservation_agent,
            "_create_success_response",
            side_effect=RuntimeError("private formatting detail"),
        ):
            response = asyncio.run(
                orchestrator.handle(
                    self.key,
                    "Ya",
                    object(),
                    self.owner,
                )
            )

        self.assertEqual(
            response,
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        orchestrator.handoff_service.require_handoff.assert_not_called()
        reservation_agent.reservation_service.create_reservation.assert_called_once()

    def test_publication_failure_through_orchestrator_creates_no_handoff(self):
        orchestrator = build_orchestrator(self.memory)
        reservation_agent = orchestrator.workflow._agents["reservation"]
        reservation_agent.reservation_service.create_reservation = MagicMock(
            return_value=persisted(906)
        )

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(
                    orchestrator.handle(
                        self.key,
                        "Ya",
                        object(),
                        self.owner,
                    )
                )

        orchestrator.handoff_service.require_handoff.assert_not_called()
        reservation_agent.reservation_service.create_reservation.assert_called_once()


class UpdatePublicationTests(unittest.TestCase):
    def setUp(self):
        self.key = "owner:update"
        self.owner = uuid4()
        self.memory = MemoryManager()
        self.memory.get_session(self.key).update(
            {
                "reservation_reference": "RSV_51515151515151515151515151515151",
                "editing_field": "people",
                "update_reservation_stage": UpdateReservationAgent.INPUT_VALUE,
                "workflow_nested": {"asked": ["people"]},
                "handoff_state": {"category": "safe-marker"},
            }
        )

    def _run(self, service):
        agent = UpdateReservationAgent(self.memory, service)
        return asyncio.run(agent.run(object(), self.key, "7 orang", self.owner))

    def test_precommit_failure_restores_selected_state_and_nested_values(self):
        before = self.memory.snapshot_conversation(self.key).materialize()

        class Service:
            def update_reservation_field_by_reference(inner_self, *_args, **_kwargs):
                state = self.memory.get_session(self.key)
                state["workflow_nested"]["asked"].append("mutated")
                raise PersistenceOperationError()

        with self.assertRaises(PersistenceOperationError):
            self._run(Service())
        self.assertEqual(self.memory.get_session(self.key), before)

    def test_unknown_outcome_clears_actionable_stage_and_preserves_handoff(self):
        class Service:
            def update_reservation_field_by_reference(self, *_args, **_kwargs):
                raise PersistenceOutcomeUnknownError()

        with self.assertRaises(PersistenceOutcomeUnknownError):
            self._run(Service())
        state = self.memory.get_session(self.key)
        self.assertIsNone(state["update_reservation_stage"])
        self.assertIsNone(state["editing_field"])
        self.assertEqual(
            state[RESERVATION_PERSISTENCE_STATE],
            {"status": OUTCOME_UNKNOWN, "operation": "update"},
        )
        self.assertEqual(state["handoff_state"], {"category": "safe-marker"})

    def test_success_clears_only_update_state(self):
        class Service:
            def update_reservation_field_by_reference(self, *_args, **_kwargs):
                return persisted(51, people=7)

        result = self._run(Service())
        state = self.memory.get_session(self.key)
        self.assertEqual(result["status"], "updated")
        self.assertIsNone(state["update_reservation_stage"])
        self.assertIsNone(state["editing_field"])
        self.assertNotIn("reservation_id", state)
        self.assertEqual(state["workflow_nested"], {"asked": ["people"]})
        self.assertEqual(state["handoff_state"], {"category": "safe-marker"})

    def test_response_failure_after_success_does_not_restore_update_stage(self):
        class Service:
            def update_reservation_field_by_reference(self, *_args, **_kwargs):
                return persisted(51, people=7)

        agent = UpdateReservationAgent(self.memory, Service())
        with patch.object(
            agent,
            "_format_reservation",
            side_effect=RuntimeError("synthetic response failure"),
        ):
            result = asyncio.run(
                agent.run(object(), self.key, "7", self.owner)
            )
        self.assertEqual(
            result["response"],
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        self.assertIsNone(
            self.memory.get_session(self.key)["update_reservation_stage"]
        )

    def test_confirmed_update_publication_failure_blocks_retry_and_allows_view(self):
        service = MagicMock()
        service.update_reservation_field_by_reference.return_value = persisted(51, people=7)
        agent = UpdateReservationAgent(self.memory, service)

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(agent.run(object(), self.key, "7", self.owner))

        retry = asyncio.run(agent.run(object(), self.key, "8", self.owner))
        self.assertEqual(
            retry["response"],
            COMMITTED_OPERATION_STATE_UNAVAILABLE_RESPONSE,
        )
        service.update_reservation_field_by_reference.assert_called_once()

        orchestrator = build_orchestrator(self.memory)
        viewed = asyncio.run(
            orchestrator.handle(
                self.key,
                "lihat reservasi saya",
                object(),
                self.owner,
            )
        )
        self.assertEqual(viewed, "Daftar reservasi terbaru")

    def test_update_formatter_failure_through_orchestrator_has_no_handoff(self):
        service = MagicMock()
        service.update_reservation_field_by_reference.return_value = persisted(51, people=7)
        orchestrator = build_orchestrator(self.memory)
        orchestrator.update_reservation_agent = UpdateReservationAgent(
            self.memory,
            service,
        )

        with patch.object(
            orchestrator.update_reservation_agent,
            "_format_reservation",
            side_effect=RuntimeError("private formatting detail"),
        ):
            response = asyncio.run(
                orchestrator.handle(
                    self.key,
                    "7",
                    object(),
                    self.owner,
                )
            )

        self.assertEqual(
            response,
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        orchestrator.handoff_service.require_handoff.assert_not_called()
        service.update_reservation_field_by_reference.assert_called_once()


class CancelPublicationTests(unittest.TestCase):
    def setUp(self):
        self.key = "owner:cancel"
        self.owner = uuid4()
        self.memory = MemoryManager()
        self.memory.get_session(self.key).update(
            {
                "cancel_reservation_reference": "RSV_63636363636363636363636363636363",
                "cancel_reservation_stage": (
                    CancelReservationAgent.CONFIRM_CANCELLATION
                ),
                "workflow_nested": {"asked": ["confirmation"]},
                "handoff_state": {"category": "safe-marker"},
            }
        )

    def _run(self, service):
        agent = CancelReservationAgent(self.memory, service)
        return asyncio.run(agent.run(object(), self.key, "Ya", self.owner))

    def test_precommit_failure_restores_cancellation_confirmation(self):
        before = self.memory.snapshot_conversation(self.key).materialize()

        class Service:
            def cancel_reservation_by_reference(inner_self, *_args, **_kwargs):
                self.memory.get_session(self.key)["workflow_nested"]["asked"].append(
                    "mutated"
                )
                raise PersistenceOperationError()

        with self.assertRaises(PersistenceOperationError):
            self._run(Service())
        self.assertEqual(self.memory.get_session(self.key), before)

    def test_unknown_outcome_clears_actionable_confirmation(self):
        class Service:
            def cancel_reservation_by_reference(self, *_args, **_kwargs):
                raise TransactionSessionUnusableError()

        with self.assertRaises(TransactionSessionUnusableError):
            self._run(Service())
        state = self.memory.get_session(self.key)
        self.assertIsNone(state["cancel_reservation_stage"])
        self.assertIsNone(state["cancel_reservation_reference"])
        self.assertEqual(
            state[RESERVATION_PERSISTENCE_STATE],
            {"status": SESSION_UNUSABLE, "operation": "cancel"},
        )

    def test_success_and_terminal_reconciliation_clear_only_cancel_state(self):
        cases = (
            SimpleNamespace(
                cancel_reservation_by_reference=lambda *_args, **_kwargs: persisted(
                    63,
                    status="cancelled",
                )
            ),
            SimpleNamespace(
                cancel_reservation_by_reference=lambda *_args, **_kwargs: None,
                get_reservation_by_reference=lambda *_args, **_kwargs: persisted(
                    63,
                    status="cancelled",
                ),
            ),
        )
        for service in cases:
            with self.subTest(terminal=hasattr(service, "get_reservation_by_reference")):
                memory = MemoryManager()
                memory.get_session(self.key).update(
                    self.memory.snapshot_conversation(self.key).materialize()
                )
                agent = CancelReservationAgent(memory, service)
                result = asyncio.run(
                    agent.run(object(), self.key, "Ya", self.owner)
                )
                state = memory.get_session(self.key)
                self.assertIsNone(state["cancel_reservation_stage"])
                self.assertIsNone(state["cancel_reservation_reference"])
                self.assertEqual(
                    state["handoff_state"],
                    {"category": "safe-marker"},
                )
                self.assertIn(
                    result["status"],
                    {"cancelled", "awaiting_cancellation"},
                )

    def test_confirmed_cancel_publication_failure_blocks_second_cancel(self):
        service = MagicMock()
        service.cancel_reservation_by_reference.return_value = persisted(
            63,
            status="cancelled",
        )
        agent = CancelReservationAgent(self.memory, service)

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(
                    agent.run(object(), self.key, "Ya", self.owner)
                )

        retry = asyncio.run(
            agent.run(object(), self.key, "Ya", self.owner)
        )
        self.assertEqual(
            retry["response"],
            COMMITTED_OPERATION_STATE_UNAVAILABLE_RESPONSE,
        )
        service.cancel_reservation_by_reference.assert_called_once()

    def test_terminal_cancel_publication_failure_blocks_second_cancel(self):
        service = MagicMock()
        service.cancel_reservation_by_reference.return_value = None
        service.get_reservation_by_reference.return_value = persisted(
            63,
            status="cancelled",
        )
        agent = CancelReservationAgent(self.memory, service)

        with patch.object(
            self.memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(
                    agent.run(object(), self.key, "Ya", self.owner)
                )

        asyncio.run(agent.run(object(), self.key, "Ya", self.owner))
        service.cancel_reservation_by_reference.assert_called_once()
        service.get_reservation_by_reference.assert_called_once()

    def test_cancel_formatter_failure_through_orchestrator_has_no_handoff(self):
        service = MagicMock()
        service.cancel_reservation_by_reference.return_value = persisted(
            63,
            status="cancelled",
        )
        orchestrator = build_orchestrator(self.memory)
        orchestrator.cancel_reservation_agent = CancelReservationAgent(
            self.memory,
            service,
        )

        with patch.object(
            orchestrator.cancel_reservation_agent,
            "_format_reservation",
            side_effect=RuntimeError("private formatting detail"),
        ):
            response = asyncio.run(
                orchestrator.handle(
                    self.key,
                    "Ya",
                    object(),
                    self.owner,
                )
            )

        self.assertEqual(
            response,
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        orchestrator.handoff_service.require_handoff.assert_not_called()
        service.cancel_reservation_by_reference.assert_called_once()


class LockBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_boundary_holds_one_lock_during_snapshot_and_replace(self):
        state = {"locked": False, "hold_calls": 0}

        class TrackingLockManager:
            @asynccontextmanager
            async def hold(self, _key):
                if state["locked"]:
                    raise AssertionError("nested acquisition")
                state["hold_calls"] += 1
                state["locked"] = True
                try:
                    yield
                finally:
                    state["locked"] = False

        memory = MemoryManager()

        class Handoff:
            ticket_service = MagicMock()

            def restore_active_handoff(self, *_args, **_kwargs):
                return None

        class Agent:
            handoff_service = Handoff()

            async def handle(inner_self, *, session_id, **_kwargs):
                self.assertTrue(state["locked"])
                snapshot = memory.snapshot_conversation(session_id)
                self.assertTrue(state["locked"])
                memory.replace_conversation(session_id, snapshot)
                return "ok"

        service = AuthenticatedChatService(
            agent=Agent(),
            lock_manager=TrackingLockManager(),
        )
        result = await service.process(
            db=object(),
            customer=SimpleNamespace(id=uuid4()),
            session_reference="chat",
            message="Halo",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(state["hold_calls"], 1)
        self.assertFalse(state["locked"])

    async def test_lock_releases_after_post_commit_publication_error(self):
        state = {"locked": False}

        class TrackingLockManager:
            @asynccontextmanager
            async def hold(self, _key):
                state["locked"] = True
                try:
                    yield
                finally:
                    state["locked"] = False

        class Handoff:
            ticket_service = MagicMock()

            def restore_active_handoff(self, *_args, **_kwargs):
                return None

        class Agent:
            handoff_service = Handoff()

            async def handle(self, **_kwargs):
                raise PostCommitMemoryPublicationError()

        service = AuthenticatedChatService(
            agent=Agent(),
            lock_manager=TrackingLockManager(),
        )
        with self.assertRaises(PostCommitMemoryPublicationError):
            await service.process(
                db=object(),
                customer=SimpleNamespace(id=uuid4()),
                session_reference="chat",
                message="Ya",
            )
        self.assertFalse(state["locked"])


class TelegramMemoryErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_commit_error_uses_safe_reply_and_log_category(self):
        db = MagicMock()
        message = SimpleNamespace(
            text="Ya",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=7, type="private"),
            effective_user=SimpleNamespace(id=7),
        )
        identity_service = SimpleNamespace(
            resolve_or_create=MagicMock(
                return_value=SimpleNamespace(id=uuid4())
            )
        )
        chat_service = SimpleNamespace(
            process=AsyncMock(
                side_effect=PostCommitMemoryPublicationError()
            )
        )
        handler = TelegramCustomerHandlers(
            identity_secret="x" * 32,
            session_factory=lambda: db,
            identity_service=identity_service,
            chat_service=chat_service,
        )

        with self.assertLogs("AURA", level=logging.INFO) as captured:
            await handler.text_message(update, None)

        message.reply_text.assert_awaited_once_with(
            MEMORY_PUBLICATION_UNAVAILABLE_REPLY
        )
        rendered = "\n".join(captured.output)
        self.assertIn("category=memory_publication_error", rendered)
        self.assertNotIn("outcome=persistence_error", rendered)
        self.assertNotIn("COMMITTED_OPERATION_STATE_UNAVAILABLE", rendered)
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
