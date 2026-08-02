from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.brain.memory_manager import MemoryManager
from app.brain.reservation_memory import (
    COMMITTED_MEMORY_UNAVAILABLE,
    RESERVATION_PERSISTENCE_STATE,
)
from app.brain.reservation_workflow_snapshot import (
    MAX_RESERVATION_IDENTIFIER,
    WORKFLOW_PAYLOAD_MAX_BYTES,
    WORKFLOW_SCHEMA_VERSION_V2,
    _validate_payload_size,
    build_workflow_snapshot_v2,
    decode_workflow_snapshot_v1,
    decode_workflow_snapshot_v2,
)
from app.core.memory_errors import (
    ConversationMemoryValidationError,
    ConversationWorkflowRecoveryError,
)
from app.core.transaction_errors import PersistenceOperationError
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
    WorkflowV1ConversionOutcome,
)


REFERENCE = "RSV_" + ("a" * 32)
MIXED_REFERENCE = "rSv_" + ("A" * 32)


def _create_payload():
    return {
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


class WorkflowSnapshotV2CodecTests(unittest.TestCase):
    def test_update_stages_round_trip_with_exact_invariants(self):
        cases = (
            {
                "update_reservation_stage": "select_reservation_reference",
                "reservation_reference": None,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": REFERENCE,
                "editing_field": None,
            },
            *(
                {
                    "update_reservation_stage": "input_value",
                    "reservation_reference": REFERENCE,
                    "editing_field": field,
                }
                for field in ("name", "people", "date", "time")
            ),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = decode_workflow_snapshot_v2(payload)
                self.assertEqual(snapshot.materialize(), payload)

    def test_cancel_stages_round_trip_with_exact_invariants(self):
        cases = (
            {
                "cancel_reservation_stage": "select_reservation_reference",
                "cancel_reservation_reference": None,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": REFERENCE,
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    decode_workflow_snapshot_v2(payload).materialize(),
                    payload,
                )

    def test_create_and_blocker_have_minimal_unchanged_v2_shapes(self):
        blocker = {
            RESERVATION_PERSISTENCE_STATE: {
                "status": COMMITTED_MEMORY_UNAVAILABLE,
                "operation": "update",
            }
        }
        for payload in (_create_payload(), blocker):
            with self.subTest(payload=payload):
                self.assertEqual(
                    decode_workflow_snapshot_v2(payload).materialize(),
                    payload,
                )

    def test_trusted_builder_canonicalizes_without_mutating_input(self):
        payload = {
            "cancel_reservation_stage": "confirm_cancellation",
            "cancel_reservation_reference": MIXED_REFERENCE,
        }
        before = deepcopy(payload)
        built = build_workflow_snapshot_v2(payload)
        self.assertEqual(payload, before)
        self.assertEqual(
            built.materialize()["cancel_reservation_reference"],
            REFERENCE,
        )
        built.materialize()["cancel_reservation_reference"] = None
        self.assertEqual(
            built.materialize()["cancel_reservation_reference"],
            REFERENCE,
        )

    def test_stored_mixed_case_reference_is_rejected(self):
        with self.assertRaises(ConversationMemoryValidationError):
            decode_workflow_snapshot_v2(
                {
                    "cancel_reservation_stage": "confirm_cancellation",
                    "cancel_reservation_reference": MIXED_REFERENCE,
                }
            )

    def test_update_invalid_shapes_are_rejected(self):
        cases = (
            {
                "update_reservation_stage": "select_reservation_reference",
                "reservation_reference": REFERENCE,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": None,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "input_value",
                "reservation_reference": REFERENCE,
                "editing_field": "status",
            },
            {
                "update_reservation_stage": "unknown",
                "reservation_reference": None,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": REFERENCE,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": REFERENCE,
                "editing_field": None,
                "extra": True,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": REFERENCE,
                "editing_field": None,
                "reservation_id": 7,
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    decode_workflow_snapshot_v2(payload)

    def test_cancel_invalid_shapes_are_rejected(self):
        cases = (
            {
                "cancel_reservation_stage": "select_reservation_reference",
                "cancel_reservation_reference": REFERENCE,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": None,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": True,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": "RSV_invalid",
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": REFERENCE,
                "cancel_reservation_id": 7,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": {"nested": REFERENCE},
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    decode_workflow_snapshot_v2(payload)

    def test_version_payload_size_and_safe_errors_are_strict(self):
        payload = _create_payload()
        invalid_versions = (1, 0, 3, -1, True, "2", None)
        for version in invalid_versions:
            with self.subTest(version=version):
                with self.assertRaises(ConversationMemoryValidationError):
                    decode_workflow_snapshot_v2(
                        payload,
                        schema_version=version,
                    )
        oversized = _create_payload()
        oversized["name"] = "x" * (WORKFLOW_PAYLOAD_MAX_BYTES + 1)
        with self.assertRaises(ConversationMemoryValidationError):
            decode_workflow_snapshot_v2(oversized)
        try:
            decode_workflow_snapshot_v2(
                {
                    "cancel_reservation_stage": "confirm_cancellation",
                    "cancel_reservation_reference": "unsafe-value",
                }
            )
        except ConversationMemoryValidationError as error:
            rendered = f"{error!s} {error!r}"
            self.assertNotIn("unsafe-value", rendered)
            self.assertNotIn(REFERENCE, rendered)
        else:
            self.fail("Malformed v2 payload was accepted.")

    def test_utf8_multibyte_and_exact_byte_boundary_are_enforced(self):
        compact = lambda value: json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

        boundary_overhead = len(compact({"value": ""}))
        at_limit = {
            "value": "x" * (WORKFLOW_PAYLOAD_MAX_BYTES - boundary_overhead)
        }
        self.assertEqual(len(compact(at_limit)), WORKFLOW_PAYLOAD_MAX_BYTES)
        _validate_payload_size(at_limit)
        one_byte_over = {"value": at_limit["value"] + "x"}
        with self.assertRaises(ConversationMemoryValidationError):
            _validate_payload_size(one_byte_over)

        multibyte = _create_payload()
        multibyte["name"] = "界" * 1400
        self.assertLess(len(json.dumps(multibyte, ensure_ascii=False)), 4096)
        self.assertGreater(len(compact(multibyte)), WORKFLOW_PAYLOAD_MAX_BYTES)
        try:
            decode_workflow_snapshot_v2(multibyte)
        except ConversationMemoryValidationError as error:
            self.assertNotIn("界", f"{error!s} {error!r}")
        else:
            self.fail("Oversized multibyte payload was accepted.")


class ImmutableWorkflowV1DecoderTests(unittest.TestCase):
    def test_all_legacy_workflow_shapes_round_trip_without_mutation(self):
        cases = (
            _create_payload(),
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
            {
                "cancel_reservation_stage": "select_reservation_id",
                "cancel_reservation_id": None,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_id": 7,
            },
            {
                RESERVATION_PERSISTENCE_STATE: {
                    "status": COMMITTED_MEMORY_UNAVAILABLE,
                    "operation": "cancel",
                }
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                before = deepcopy(payload)
                decoded = decode_workflow_snapshot_v1(payload)
                self.assertEqual(payload, before)
                self.assertEqual(decoded.materialize(), before)

    def test_v1_rejects_v2_keys_types_ranges_and_corrupt_combinations(self):
        cases = (
            {
                "update_reservation_stage": "select_field",
                "reservation_reference": REFERENCE,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_id": True,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_id": 0,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_id": -1,
                "editing_field": None,
            },
            {
                "update_reservation_stage": "select_field",
                "reservation_id": MAX_RESERVATION_IDENTIFIER + 1,
                "editing_field": None,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_id": "7",
            },
            {
                "cancel_reservation_stage": "select_reservation_id",
                "cancel_reservation_id": 7,
            },
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_id": 7,
                "extra": None,
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    decode_workflow_snapshot_v1(payload)

    def test_v1_errors_do_not_reflect_legacy_identifier(self):
        value = MAX_RESERVATION_IDENTIFIER + 1
        try:
            decode_workflow_snapshot_v1(
                {
                    "update_reservation_stage": "select_field",
                    "reservation_id": value,
                    "editing_field": None,
                }
            )
        except ConversationMemoryValidationError as error:
            self.assertNotIn(str(value), f"{error!s} {error!r}")
        else:
            self.fail("Unsafe v1 identifier was accepted.")


class _TransactionDB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _WorkflowRepository:
    def __init__(self, row=None):
        self.row = row
        self.for_update = None

    def get_by_scope(self, _db, **kwargs):
        self.for_update = kwargs.get("for_update")
        return self.row

    @staticmethod
    def replace(row, *, schema_version, payload, is_active):
        row.schema_version = schema_version
        row.payload = deepcopy(payload)
        row.is_active = is_active
        row.revision += 1


class _ReservationRepository:
    def __init__(self, values=None, *, fail=False):
        self.values = values or {}
        self.fail = fail
        self.calls = []

    def get_by_id(self, _db, reservation_id, owner_customer_id):
        self.calls.append((reservation_id, owner_customer_id))
        if self.fail:
            raise RuntimeError("private database failure")
        return self.values.get((owner_customer_id, reservation_id))


class WorkflowV1ConversionTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid4()
        self.other_owner = uuid4()
        self.memory_key = "trusted-internal-memory-key"

    @staticmethod
    def _row(payload, *, version=1, revision=4, active=True):
        return SimpleNamespace(
            schema_version=version,
            payload=deepcopy(payload),
            is_active=active,
            revision=revision,
        )

    def _service(self, row, reservations=None, *, fail=False):
        workflow = _WorkflowRepository(row)
        reservation = _ReservationRepository(reservations, fail=fail)
        service = ConversationWorkflowStateService(
            MemoryManager(),
            repository=workflow,
            reservation_repository=reservation,
        )
        return service, workflow, reservation

    def _convert(self, service, db, *, revision=4):
        return service.convert_v1_state_to_v2(
            db,
            owner_customer_id=self.owner,
            memory_key=self.memory_key,
            expected_revision=revision,
        )

    def test_no_selection_update_and_cancel_convert_without_lookup(self):
        cases = (
            (
                {
                    "update_reservation_stage": "select_reservation_id",
                    "reservation_id": None,
                    "editing_field": None,
                },
                {
                    "update_reservation_stage": (
                        "select_reservation_reference"
                    ),
                    "reservation_reference": None,
                    "editing_field": None,
                },
            ),
            (
                {
                    "cancel_reservation_stage": "select_reservation_id",
                    "cancel_reservation_id": None,
                },
                {
                    "cancel_reservation_stage": (
                        "select_reservation_reference"
                    ),
                    "cancel_reservation_reference": None,
                },
            ),
        )
        for source, target in cases:
            with self.subTest(source=source):
                row = self._row(source)
                service, workflow, reservation = self._service(row)
                outcome = self._convert(service, _TransactionDB())
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.CONVERTED,
                )
                self.assertEqual(row.schema_version, 2)
                self.assertEqual(row.payload, target)
                self.assertEqual(row.revision, 5)
                self.assertTrue(row.is_active)
                self.assertTrue(workflow.for_update)
                self.assertEqual(reservation.calls, [])

    def test_selected_update_and_cancel_use_owner_scoped_lookup(self):
        reservations = {
            (self.owner, 7): SimpleNamespace(public_reference=REFERENCE),
        }
        cases = (
            (
                {
                    "update_reservation_stage": "input_value",
                    "reservation_id": 7,
                    "editing_field": "people",
                },
                "reservation_reference",
            ),
            (
                {
                    "cancel_reservation_stage": "confirm_cancellation",
                    "cancel_reservation_id": 7,
                },
                "cancel_reservation_reference",
            ),
        )
        for payload, reference_key in cases:
            with self.subTest(payload=payload):
                row = self._row(payload)
                service, _workflow, reservation = self._service(
                    row,
                    reservations,
                )
                outcome = self._convert(service, _TransactionDB())
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.CONVERTED,
                )
                self.assertEqual(row.payload[reference_key], REFERENCE)
                self.assertNotIn("reservation_id", row.payload)
                self.assertNotIn("cancel_reservation_id", row.payload)
                self.assertEqual(reservation.calls, [(7, self.owner)])

    def test_missing_cross_owner_null_and_invalid_reference_are_equivalent(self):
        source = {
            "update_reservation_stage": "select_field",
            "reservation_id": 7,
            "editing_field": None,
        }
        cases = (
            {},
            {(self.other_owner, 7): SimpleNamespace(public_reference=REFERENCE)},
            {(self.owner, 7): SimpleNamespace(public_reference=None)},
            {(self.owner, 7): SimpleNamespace(public_reference="invalid")},
            {(self.owner, 7): SimpleNamespace(public_reference=MIXED_REFERENCE)},
        )
        for reservations in cases:
            with self.subTest(reservations=reservations):
                row = self._row(source)
                service, _workflow, _reservation = self._service(
                    row,
                    reservations,
                )
                outcome = self._convert(service, _TransactionDB())
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.UNAVAILABLE,
                )
                self.assertEqual(row.schema_version, 2)
                self.assertEqual(row.payload, {})
                self.assertFalse(row.is_active)
                self.assertEqual(row.revision, 5)

    def test_cancelled_row_with_valid_reference_remains_convertible(self):
        row = self._row(
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_id": 7,
            }
        )
        reservations = {
            (self.owner, 7): SimpleNamespace(
                public_reference=REFERENCE,
                status="cancelled",
            )
        }
        service, _workflow, _reservation = self._service(row, reservations)
        self.assertEqual(
            self._convert(service, _TransactionDB()),
            WorkflowV1ConversionOutcome.CONVERTED,
        )

    def test_create_blocker_and_inactive_v1_convert_without_semantic_change(self):
        cases = (
            (_create_payload(), True),
            (
                {
                    RESERVATION_PERSISTENCE_STATE: {
                        "status": COMMITTED_MEMORY_UNAVAILABLE,
                        "operation": "create",
                    }
                },
                True,
            ),
            ({}, False),
        )
        for payload, active in cases:
            with self.subTest(payload=payload, active=active):
                row = self._row(payload, active=active)
                service, _workflow, _reservation = self._service(row)
                outcome = self._convert(service, _TransactionDB())
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.CONVERTED,
                )
                self.assertEqual(row.schema_version, 2)
                self.assertEqual(row.payload, payload)
                self.assertEqual(row.is_active, active)

    def test_already_v2_is_validated_and_left_unchanged(self):
        payload = {
            "cancel_reservation_stage": "confirm_cancellation",
            "cancel_reservation_reference": REFERENCE,
        }
        row = self._row(payload, version=2)
        before = deepcopy(vars(row))
        service, _workflow, _reservation = self._service(row)
        outcome = self._convert(service, _TransactionDB())
        self.assertEqual(
            outcome,
            WorkflowV1ConversionOutcome.ALREADY_V2,
        )
        self.assertEqual(vars(row), before)

    def test_inactive_v2_tombstone_is_an_already_v2_noop(self):
        row = self._row({}, version=2, active=False)
        before = deepcopy(vars(row))
        service, workflow, reservation = self._service(row)
        outcome = self._convert(service, _TransactionDB())
        self.assertEqual(
            outcome,
            WorkflowV1ConversionOutcome.ALREADY_V2,
        )
        self.assertEqual(vars(row), before)
        self.assertTrue(workflow.for_update)
        self.assertEqual(reservation.calls, [])

    def test_invalid_memory_keys_fail_before_hash_or_query(self):
        invalid_keys = (
            "",
            "   ",
            " leading",
            "trailing ",
            "control\nkey",
            "a" * 257,
            None,
            True,
            7,
            b"bytes",
        )
        for memory_key in invalid_keys:
            with self.subTest(kind=type(memory_key).__name__):
                row = self._row(_create_payload())
                service, workflow, reservation = self._service(row)
                with patch.object(service, "hash_session_reference") as hasher:
                    try:
                        service.convert_v1_state_to_v2(
                            _TransactionDB(),
                            owner_customer_id=self.owner,
                            memory_key=memory_key,
                            expected_revision=4,
                        )
                    except ConversationWorkflowRecoveryError as error:
                        rendered = f"{error!s} {error!r}"
                        if isinstance(memory_key, str) and memory_key:
                            self.assertNotIn(memory_key, rendered)
                    else:
                        self.fail("Invalid conversion memory key was accepted.")
                    hasher.assert_not_called()
                self.assertIsNone(workflow.for_update)
                self.assertEqual(reservation.calls, [])

    def test_existing_internal_memory_key_forms_remain_valid(self):
        valid_keys = (
            self.memory_key,
            f"{self.owner}:session.reference-1",
            "demo-session-1",
        )
        for index, memory_key in enumerate(valid_keys):
            with self.subTest(case=index):
                row = self._row(_create_payload())
                service, workflow, _reservation = self._service(row)
                outcome = service.convert_v1_state_to_v2(
                    _TransactionDB(),
                    owner_customer_id=self.owner,
                    memory_key=memory_key,
                    expected_revision=4,
                )
                self.assertEqual(
                    outcome,
                    WorkflowV1ConversionOutcome.CONVERTED,
                )
                self.assertTrue(workflow.for_update)

    def test_revision_conflict_does_not_overwrite_newer_state(self):
        row = self._row(_create_payload(), revision=5)
        before = deepcopy(vars(row))
        service, _workflow, _reservation = self._service(row)
        outcome = self._convert(service, _TransactionDB(), revision=4)
        self.assertEqual(
            outcome,
            WorkflowV1ConversionOutcome.REVISION_CONFLICT,
        )
        self.assertEqual(vars(row), before)

    def test_unsupported_or_corrupt_state_fails_closed_without_mutation(self):
        cases = (
            self._row(_create_payload(), version=3),
            self._row({"private": "payload"}),
            self._row(_create_payload(), version=True),
            self._row(
                {
                    "update_reservation_stage": "select_field",
                    "reservation_id": 7,
                    "editing_field": None,
                },
                version=2,
            ),
            self._row(
                {
                    "cancel_reservation_stage": "select_reservation_reference",
                    "cancel_reservation_reference": None,
                },
                version=1,
            ),
        )
        for index, row in enumerate(cases):
            with self.subTest(case=index):
                before = deepcopy(vars(row))
                service, _workflow, _reservation = self._service(row)
                with self.assertRaises(ConversationWorkflowRecoveryError):
                    self._convert(service, _TransactionDB())
                self.assertEqual(vars(row), before)

    def test_active_v1_writer_revalidates_before_repository_write(self):
        row = None
        service, workflow, _reservation = self._service(row)
        v2_snapshot = build_workflow_snapshot_v2(
            {
                "cancel_reservation_stage": "select_reservation_reference",
                "cancel_reservation_reference": None,
            }
        )
        with self.assertRaises(ConversationMemoryValidationError):
            service._write_snapshot(
                _TransactionDB(),
                owner_customer_id=self.owner,
                memory_key=self.memory_key,
                snapshot=v2_snapshot,
            )
        self.assertIsNone(workflow.for_update)

    def test_database_failure_rolls_back_and_preserves_v1_row(self):
        row = self._row(
            {
                "update_reservation_stage": "select_field",
                "reservation_id": 7,
                "editing_field": None,
            }
        )
        before = deepcopy(vars(row))
        service, _workflow, _reservation = self._service(row, fail=True)
        db = _TransactionDB()
        with self.assertRaises(PersistenceOperationError):
            self._convert(service, db)
        self.assertEqual(vars(row), before)
        self.assertEqual(db.rollbacks, 1)

    def test_active_restore_remains_v1_only_during_phase_a(self):
        row = self._row(
            {
                "cancel_reservation_stage": "confirm_cancellation",
                "cancel_reservation_reference": REFERENCE,
            },
            version=WORKFLOW_SCHEMA_VERSION_V2,
        )
        service, _workflow, _reservation = self._service(row)
        with self.assertRaises(ConversationWorkflowRecoveryError):
            service.restore(
                _TransactionDB(),
                owner_customer_id=self.owner,
                memory_key=self.memory_key,
            )

    def test_model_accepts_exact_versions_one_and_two(self):
        constraint = next(
            item
            for item in ConversationWorkflowState.__table__.constraints
            if item.name == "ck_conversation_workflow_states_schema_version"
        )
        self.assertEqual(str(constraint.sqltext), "schema_version IN (1, 2)")


if __name__ == "__main__":
    unittest.main()
