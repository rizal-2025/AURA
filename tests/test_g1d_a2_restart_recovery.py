import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from uuid import uuid4

from app.agents.reservation_agent import ReservationAgent
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    COMMITTED_MEMORY_UNAVAILABLE,
    RESERVATION_PERSISTENCE_STATE,
)
from app.brain.reservation_workflow_snapshot import (
    WORKFLOW_PAYLOAD_MAX_BYTES,
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_VERSION_V2,
    capture_reservation_workflow_snapshot_v2,
    validate_persisted_workflow_snapshot_v1,
)
from app.core.conversation_lock_manager import ConversationLockManager
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.memory_errors import (
    ConversationMemoryValidationError,
    ConversationWorkflowPublicationError,
    ConversationWorkflowRecoveryError,
)
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)


def create_payload(**changes):
    payload = {
        "intent": "reservation",
        "name": None,
        "people": None,
        "date": None,
        "time": None,
        "completed": False,
        "awaiting_confirmation": False,
        "editing_field": None,
        "asked_fields": ["name"],
    }
    payload.update(changes)
    return payload


class WorkflowSnapshotValidationTests(unittest.TestCase):
    def test_valid_create_collection_confirmation_and_edit_round_trip(self):
        cases = [
            create_payload(),
            create_payload(
                name="Ayu",
                people=4,
                date="2026-08-20",
                time="19:00",
                awaiting_confirmation=True,
                asked_fields=["name", "people", "date", "time"],
            ),
            create_payload(
                name="Ayu",
                people=4,
                date="2026-08-20",
                time="19:00",
                awaiting_confirmation=True,
                editing_field="people",
                asked_fields=["name", "people", "date", "time"],
            ),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = validate_persisted_workflow_snapshot_v1(
                    payload,
                    schema_version=WORKFLOW_SCHEMA_VERSION,
                )
                self.assertEqual(snapshot.materialize(), payload)

    def test_valid_update_stages_round_trip(self):
        cases = [
            {
                "update_reservation_stage": "select_reservation_id",
                "reservation_id": None,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_id": 7,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "input_value",
                "reservation_id": 7,
                "editing_field": "people",
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = validate_persisted_workflow_snapshot_v1(
                    payload,
                    schema_version=WORKFLOW_SCHEMA_VERSION,
                )
                self.assertEqual(snapshot.materialize(), payload)

    def test_valid_cancel_stages_round_trip(self):
        cases = [
            {
                "cancel_reservation_stage": "select_reservation_id",
                "cancel_reservation_id": None,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_id": 7,
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = validate_persisted_workflow_snapshot_v1(
                    payload,
                    schema_version=WORKFLOW_SCHEMA_VERSION,
                )
                self.assertEqual(snapshot.materialize(), payload)

    def test_blocker_round_trips(self):
        payload = {
            RESERVATION_PERSISTENCE_STATE: {
                "status": COMMITTED_MEMORY_UNAVAILABLE,
                "operation": "create",
            }
        }
        snapshot = validate_persisted_workflow_snapshot_v1(
            payload,
            schema_version=WORKFLOW_SCHEMA_VERSION,
        )
        self.assertEqual(snapshot.materialize(), payload)

    def test_extra_handoff_and_scope_keys_are_rejected(self):
        forbidden = (
            ("extra", "value"),
            ("handoff_required", True),
            ("handoff_state", {}),
            ("owner_customer_id", str(uuid4())),
            ("session_reference", "private"),
        )
        for key, value in forbidden:
            with self.subTest(key=key):
                payload = create_payload()
                payload[key] = value
                with self.assertRaises(ConversationMemoryValidationError):
                    validate_persisted_workflow_snapshot_v1(
                        payload,
                        schema_version=WORKFLOW_SCHEMA_VERSION,
                    )

    def test_invalid_schema_stage_bool_identifier_and_combinations_rejected(self):
        cases = [
            (create_payload(), 2),
            (
                {
                    "update_reservation_stage": "unknown",
                    "reservation_id": None,
                    "editing_field": None,
                },
                1,
            ),
            (create_payload(completed=1), 1),
            (
                {
                    "update_reservation_stage": "select_field",
                    "reservation_id": True,
                    "editing_field": None,
                },
                1,
            ),
            (
                create_payload(
                    awaiting_confirmation=True,
                    editing_field="people",
                ),
                1,
            ),
        ]
        for payload, schema_version in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    validate_persisted_workflow_snapshot_v1(
                        payload,
                        schema_version=schema_version,
                    )

        memory = MemoryManager()
        state = memory.get_session("mixed")
        state["update_reservation_stage"] = "select_reservation_reference"
        state["cancel_reservation_stage"] = "select_reservation_reference"
        with self.assertRaises(ConversationMemoryValidationError):
            capture_reservation_workflow_snapshot_v2(memory, "mixed")

    def test_oversized_payload_is_rejected(self):
        payload = create_payload(name="A" * (WORKFLOW_PAYLOAD_MAX_BYTES + 1))
        with self.assertRaises(ConversationMemoryValidationError):
            validate_persisted_workflow_snapshot_v1(
                payload,
                schema_version=WORKFLOW_SCHEMA_VERSION,
            )

    def test_capture_excludes_arbitrary_and_handoff_state(self):
        memory = MemoryManager()
        state = memory.get_session("safe")
        state.update(create_payload(name="Ayu"))
        state.update(
            {
                "handoff_required": True,
                "handoff_state": {"ticket_number": "CS-2026-000001"},
                "raw_message": "private",
                "intent_confidence": 0.99,
            }
        )
        payload = capture_reservation_workflow_snapshot_v2(
            memory,
            "safe",
        ).materialize()
        self.assertEqual(set(payload), set(create_payload()))
        self.assertNotIn("handoff_required", payload)
        self.assertNotIn("raw_message", payload)

    def test_snapshot_does_not_alias_live_or_materialized_state(self):
        payload = create_payload(asked_fields=["name", "people"])
        snapshot = validate_persisted_workflow_snapshot_v1(
            payload,
            schema_version=WORKFLOW_SCHEMA_VERSION,
        )
        payload["asked_fields"].append("date")
        first = snapshot.materialize()
        first["asked_fields"].append("date")
        self.assertEqual(
            snapshot.materialize()["asked_fields"],
            ["name", "people"],
        )

    def test_terminal_create_has_no_persisted_actionable_snapshot(self):
        memory = MemoryManager()
        memory.get_session("done").update(
            create_payload(
                name="Ayu",
                people=4,
                date="2026-08-20",
                time="19:00",
                completed=True,
                asked_fields=["name", "people", "date", "time"],
            )
        )
        self.assertIsNone(
            capture_reservation_workflow_snapshot_v2(memory, "done")
        )


class ModelDeclarationTests(unittest.TestCase):
    def test_model_declares_stable_constraints_and_indexes(self):
        table = ConversationWorkflowState.__table__
        constraint_names = {
            constraint.name for constraint in table.constraints
        }
        self.assertTrue(
            {
                "pk_conversation_workflow_states",
                "fk_conversation_workflow_states_owner_customer_id_customers",
                "uq_conversation_workflow_states_owner_session",
                "ck_conversation_workflow_states_schema_version",
                "ck_conversation_workflow_states_revision",
                "ck_conversation_workflow_states_session_hash_length",
                "ck_conversation_workflow_states_payload_object",
            }.issubset(constraint_names)
        )
        self.assertEqual(
            {index.name for index in table.indexes},
            {
                "ix_conversation_workflow_states_owner_customer_id",
                "ix_conversation_workflow_states_updated_at",
            },
        )
        self.assertEqual(table.c.session_reference_hash.type.length, 64)
        self.assertFalse(table.c.payload.nullable)


class TransactionDB:
    def commit(self):
        pass

    def rollback(self):
        pass


class InMemoryWorkflowRepository:
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def get_by_scope(
        self,
        _db,
        *,
        owner_customer_id,
        session_reference_hash,
        for_update=False,
    ):
        return self.rows.get((owner_customer_id, session_reference_hash))

    def create(
        self,
        _db,
        *,
        owner_customer_id,
        session_reference_hash,
        schema_version,
        payload,
        is_active,
    ):
        key = (owner_customer_id, session_reference_hash)
        if key in self.rows:
            raise RuntimeError("duplicate")
        row = SimpleNamespace(
            id=self.next_id,
            owner_customer_id=owner_customer_id,
            session_reference_hash=session_reference_hash,
            schema_version=schema_version,
            payload=deepcopy(payload),
            is_active=is_active,
            revision=1,
        )
        self.next_id += 1
        self.rows[key] = row
        return row

    @staticmethod
    def replace(row, *, schema_version, payload, is_active):
        row.schema_version = schema_version
        row.payload = deepcopy(payload)
        row.is_active = is_active
        row.revision += 1


class WorkflowStateServiceTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid4()
        self.memory_key = build_authenticated_memory_key(
            self.owner,
            "restart",
        )
        self.repository = InMemoryWorkflowRepository()
        self.db = TransactionDB()

    def service(self, memory=None):
        return ConversationWorkflowStateService(
            memory or MemoryManager(),
            repository=self.repository,
        )

    def test_restart_restores_create_update_cancel_and_blocker(self):
        payloads = [
            create_payload(
                name="Ayu",
                people=4,
                asked_fields=["name", "people", "date"],
            ),
            {
                "update_reservation_stage": "input_value",
                "reservation_reference": f"RSV_{7:032x}",
                "editing_field": "people",
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": f"RSV_{7:032x}",
            },
            {
                RESERVATION_PERSISTENCE_STATE: {
                    "status": COMMITTED_MEMORY_UNAVAILABLE,
                    "operation": "update",
                }
            },
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                raw_session = f"restart-{index}"
                key = build_authenticated_memory_key(
                    self.owner,
                    raw_session,
                )
                first_memory = MemoryManager()
                first_memory.replace_reservation_workflow_state(key, payload)
                first = self.service(first_memory)
                first.publish(
                    self.db,
                    owner_customer_id=self.owner,
                    memory_key=key,
                )

                restarted_memory = MemoryManager()
                restarted = self.service(restarted_memory)
                restarted.restore(
                    self.db,
                    owner_customer_id=self.owner,
                    memory_key=key,
                )
                restored = capture_reservation_workflow_snapshot_v2(
                    restarted_memory,
                    key,
                )
                self.assertEqual(restored.materialize(), payload)

    def test_missing_row_cleans_workflow_but_preserves_handoff(self):
        memory = MemoryManager()
        state = memory.get_session(self.memory_key)
        state.update(create_payload(name="Ayu"))
        state["handoff_required"] = True
        service = self.service(memory)
        service.restore(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )
        restored = memory.get_session(self.memory_key)
        self.assertIsNone(restored["intent"])
        self.assertTrue(restored["handoff_required"])

    def test_corrupted_row_fails_closed_without_loading_payload(self):
        service = self.service()
        session_hash = service.hash_session_reference(self.memory_key)
        self.repository.rows[(self.owner, session_hash)] = SimpleNamespace(
            schema_version=1,
            payload={"handoff_required": True},
            is_active=True,
            revision=1,
        )
        with self.assertRaises(ConversationWorkflowRecoveryError):
            service.restore(
                self.db,
                owner_customer_id=self.owner,
                memory_key=self.memory_key,
            )
        self.assertIsNone(
            service.memory_manager.get_session(self.memory_key)["intent"]
        )

    def test_terminal_publication_leaves_inactive_tombstone(self):
        memory = MemoryManager()
        memory.replace_reservation_workflow_state(
            self.memory_key,
            create_payload(),
        )
        service = self.service(memory)
        service.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )
        memory.get_session(self.memory_key)["completed"] = True
        service.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )
        session_hash = service.hash_session_reference(self.memory_key)
        row = self.repository.rows[(self.owner, session_hash)]
        self.assertFalse(row.is_active)
        self.assertEqual(row.payload, {})
        self.assertEqual(row.revision, 2)
        self.assertEqual(row.schema_version, WORKFLOW_SCHEMA_VERSION_V2)

    def test_rejected_create_publishes_inactive_and_cannot_restore_after_restart(self):
        memory = MemoryManager()
        memory.replace_reservation_workflow_state(
            self.memory_key,
            create_payload(
                name="Ayu",
                people=4,
                date="2026-08-20",
                time="19:00",
                awaiting_confirmation=True,
                asked_fields=["name", "people", "date", "time"],
            ),
        )
        service = self.service(memory)
        service.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )

        agent = ReservationAgent(memory_manager=memory)
        result = asyncio.run(
            agent.handle_confirmation("nggak jadi", self.memory_key)
        )
        service.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )

        restarted_memory = MemoryManager()
        restarted = self.service(restarted_memory)
        restarted.restore(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )
        restored = restarted_memory.get_session(self.memory_key)
        session_hash = service.hash_session_reference(self.memory_key)
        row = self.repository.rows[(self.owner, session_hash)]

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(row.is_active)
        self.assertEqual(row.payload, {})
        self.assertIsNone(
            capture_reservation_workflow_snapshot_v2(
                restarted_memory,
                self.memory_key,
            )
        )
        self.assertIsNone(restored["intent"])
        self.assertFalse(restored.get("awaiting_confirmation", False))
        self.assertIsNone(restored["name"])
        self.assertIsNone(restored["people"])
        self.assertIsNone(restored["date"])
        self.assertIsNone(restored["time"])

    def test_stale_writer_cannot_resurrect_terminal_workflow(self):
        initial_memory = MemoryManager()
        initial_memory.replace_reservation_workflow_state(
            self.memory_key,
            create_payload(),
        )
        initial = self.service(initial_memory)
        initial.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )

        winner_memory = MemoryManager()
        stale_memory = MemoryManager()
        winner = self.service(winner_memory)
        stale = self.service(stale_memory)
        for service in (winner, stale):
            service.restore(
                self.db,
                owner_customer_id=self.owner,
                memory_key=self.memory_key,
            )

        winner_memory.get_session(self.memory_key)["completed"] = True
        winner.publish(
            self.db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
        )
        with self.assertRaises(ConversationWorkflowPublicationError):
            stale.publish(
                self.db,
                owner_customer_id=self.owner,
                memory_key=self.memory_key,
            )
        session_hash = winner.hash_session_reference(self.memory_key)
        row = self.repository.rows[(self.owner, session_hash)]
        self.assertFalse(row.is_active)
        self.assertEqual(row.payload, {})

    def test_same_raw_session_for_different_customers_is_isolated(self):
        owner_two = uuid4()
        for owner, name in ((self.owner, "Ayu"), (owner_two, "Budi")):
            key = build_authenticated_memory_key(owner, "shared")
            memory = MemoryManager()
            memory.replace_reservation_workflow_state(
                key,
                create_payload(name=name),
            )
            self.service(memory).publish(
                self.db,
                owner_customer_id=owner,
                memory_key=key,
            )
        self.assertEqual(len(self.repository.rows), 2)


class AuthenticatedRecoveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_handoff_agent_publication_order_is_serialized(self):
        events = []

        class WorkflowState:
            def restore(self, *_args, **_kwargs):
                events.append("workflow_restore")

            def publish(self, *_args, **_kwargs):
                events.append("workflow_publish")

        class Handoff:
            def restore_active_handoff(self, *_args):
                events.append("handoff_restore")

            @staticmethod
            def recovery_error_response():
                return "safe"

        class Agent:
            handoff_service = Handoff()

            async def handle(self, **_kwargs):
                events.append("agent")
                return "ok"

        service = AuthenticatedChatService(
            agent=Agent(),
            lock_manager=ConversationLockManager(),
            workflow_state_service=WorkflowState(),
        )
        response = await service.process(
            db=object(),
            customer=SimpleNamespace(id=uuid4()),
            session_reference="ordered",
            message="halo",
        )
        self.assertEqual(response, "ok")
        self.assertEqual(
            events,
            [
                "workflow_restore",
                "handoff_restore",
                "agent",
                "workflow_publish",
            ],
        )

    async def test_recovery_failure_prevents_handoff_agent_and_publication(self):
        events = []

        class WorkflowState:
            def restore(self, *_args, **_kwargs):
                events.append("workflow_restore")
                raise ConversationWorkflowRecoveryError()

            def publish(self, *_args, **_kwargs):
                events.append("workflow_publish")

        class Handoff:
            def restore_active_handoff(self, *_args):
                events.append("handoff_restore")

        class Agent:
            handoff_service = Handoff()

            async def handle(self, **_kwargs):
                events.append("agent")
                return "unsafe"

        service = AuthenticatedChatService(
            agent=Agent(),
            workflow_state_service=WorkflowState(),
        )
        with self.assertRaises(ConversationWorkflowRecoveryError):
            await service.process(
                db=object(),
                customer=SimpleNamespace(id=uuid4()),
                session_reference="corrupt",
                message="buat reservasi",
            )
        self.assertEqual(events, ["workflow_restore"])

    async def test_publication_failure_does_not_return_agent_response(self):
        class WorkflowState:
            def restore(self, *_args, **_kwargs):
                pass

            def publish(self, *_args, **_kwargs):
                raise ConversationWorkflowPublicationError()

        class Handoff:
            def restore_active_handoff(self, *_args):
                pass

        class Agent:
            handoff_service = Handoff()

            async def handle(self, **_kwargs):
                return "must not escape"

        service = AuthenticatedChatService(
            agent=Agent(),
            workflow_state_service=WorkflowState(),
        )
        with self.assertRaises(ConversationWorkflowPublicationError):
            await service.process(
                db=object(),
                customer=SimpleNamespace(id=uuid4()),
                session_reference="publication-failure",
                message="buat reservasi",
            )


if __name__ == "__main__":
    unittest.main()
