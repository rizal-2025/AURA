import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_workflow_snapshot import (
    build_workflow_snapshot_v2,
    capture_reservation_workflow_snapshot_v2,
    decode_workflow_snapshot_v2,
)
from app.core.memory_errors import ConversationMemoryValidationError


OWNER = "selection-owner"
OTHER_OWNER = "other-owner"


def reference_for(index: int) -> str:
    return f"RSV_{index:032x}"


def reservation(index: int, *, owner=OWNER, status="pending"):
    return SimpleNamespace(
        id=index,
        reference=reference_for(index),
        name=f"Tamu {index}",
        people=index,
        date=f"2026-08-{9 + index:02d}",
        time=f"{8 + index:02d}:00",
        status=status,
        owner_customer_id=owner,
    )


class SelectionService:
    def __init__(self, rows=()):
        self.rows = {row.reference: row for row in rows}
        self.update_calls = []
        self.cancel_calls = []

    def list_selectable_reservations(self, _db, owner_customer_id, limit=5):
        rows = (
            row
            for row in self.rows.values()
            if row.owner_customer_id == owner_customer_id
            and row.status.lower() != "cancelled"
        )
        return tuple(sorted(rows, key=lambda row: row.id, reverse=True)[:limit])

    def get_selectable_reservation_by_reference(
        self,
        _db,
        public_reference,
        owner_customer_id,
    ):
        row = self.rows.get(public_reference)
        if (
            row is None
            or row.owner_customer_id != owner_customer_id
            or row.status.lower() == "cancelled"
        ):
            return None
        return row

    def get_reservation_by_reference(self, _db, public_reference, owner_customer_id):
        row = self.rows.get(public_reference)
        return row if row is not None and row.owner_customer_id == owner_customer_id else None

    def update_reservation_field_by_reference(
        self,
        _db,
        public_reference,
        field_name,
        new_value,
        owner_customer_id,
    ):
        row = self.get_selectable_reservation_by_reference(
            _db,
            public_reference,
            owner_customer_id,
        )
        if row is None:
            return None
        setattr(row, field_name, new_value)
        self.update_calls.append((public_reference, field_name, new_value))
        return row

    def cancel_reservation_by_reference(
        self,
        _db,
        public_reference,
        owner_customer_id,
    ):
        row = self.get_selectable_reservation_by_reference(
            _db,
            public_reference,
            owner_customer_id,
        )
        if row is None:
            return None
        row.status = "cancelled"
        self.cancel_calls.append(public_reference)
        return row


class ReservationSelectionUxTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    @staticmethod
    def send(agent, session_id, message, owner=OWNER):
        return asyncio.run(agent.run(MagicMock(), session_id, message, owner))

    def update_agent(self, rows):
        memory = MemoryManager()
        service = SelectionService(rows)
        return memory, service, UpdateReservationAgent(memory, service)

    def cancel_agent(self, rows):
        memory = MemoryManager()
        service = SelectionService(rows)
        return memory, service, CancelReservationAgent(memory, service)

    def test_update_zero_reservations_ends_safely(self):
        memory, service, agent = self.update_agent([reservation(1, status="cancelled")])
        result = self.send(agent, "u-zero", "ubah reservasi")
        self.assertEqual(result["status"], "no_reservations")
        self.assertIn("tidak menemukan reservasi aktif", result["response"])
        self.assertIsNone(memory.get_session("u-zero").get("update_reservation_stage"))
        self.assertEqual(service.update_calls, [])

    def test_update_single_yes_uses_internal_reference(self):
        row = reservation(1)
        memory, service, agent = self.update_agent([row])
        start = self.send(agent, "u-one", "ubah reservasi")
        self.assertNotIn("RSV_", start["response"])
        self.assertIn("10 Agu 2026", start["response"])
        self.assertEqual(
            memory.get_session("u-one")["update_reservation_stage"],
            agent.CONFIRM_RESERVATION_SELECTION,
        )
        self.send(agent, "u-one", "Ya")
        self.send(agent, "u-one", "people")
        result = self.send(agent, "u-one", "4")
        self.assertEqual(result["status"], "updated")
        self.assertEqual(service.update_calls, [(row.reference, "people", 4)])

    def test_update_single_no_and_invalid_retry_are_safe(self):
        row = reservation(1)
        memory, service, agent = self.update_agent([row])
        self.send(agent, "u-retry", "ubah reservasi")
        invalid = self.send(agent, "u-retry", "mungkin")
        self.assertTrue(invalid["invalid_input"])
        self.assertEqual(
            memory.get_session("u-retry")["reservation_reference"],
            row.reference,
        )
        self.send(agent, "u-retry", "Ya")
        self.assertEqual(
            memory.get_session("u-retry")["update_reservation_stage"],
            agent.SELECT_FIELD,
        )

        self.send(agent, "u-no", "ubah reservasi")
        rejected = self.send(agent, "u-no", "Tidak")
        self.assertEqual(rejected["status"], "update_rejected")
        self.assertEqual(service.update_calls, [])
        self.assertIsNone(memory.get_session("u-no")["update_reservation_stage"])

    def test_update_multiple_selects_first_and_non_first_by_number(self):
        rows = [reservation(1), reservation(2), reservation(3)]
        for choice, expected in (("1", reference_for(3)), ("2", reference_for(2))):
            with self.subTest(choice=choice):
                memory, service, agent = self.update_agent(rows)
                start = self.send(agent, f"u-{choice}", "ubah reservasi")
                self.assertNotIn("RSV_", start["response"])
                self.send(agent, f"u-{choice}", choice)
                state = memory.get_session(f"u-{choice}")
                self.assertEqual(state["reservation_reference"], expected)
                self.assertEqual(state["update_reservation_stage"], agent.SELECT_FIELD)
                self.assertEqual(service.update_calls, [])

    def test_update_multiple_rejects_invalid_choices_without_mutation(self):
        memory, service, agent = self.update_agent([reservation(1), reservation(2)])
        self.send(agent, "u-invalid", "ubah reservasi")
        for value in ("0", "3", "-1", "pilih yang sore"):
            with self.subTest(value=value):
                result = self.send(agent, "u-invalid", value)
                self.assertTrue(result["invalid_input"])
                self.assertIn("1 sampai 2", result["response"])
                self.assertEqual(service.update_calls, [])

    def test_update_stale_candidate_refreshes_and_ownership_isolated(self):
        owned = reservation(1)
        foreign = reservation(2, owner=OTHER_OWNER)
        memory, service, agent = self.update_agent([owned, reservation(3), foreign])
        self.send(agent, "u-stale", "ubah reservasi")
        service.rows[reference_for(3)].status = "cancelled"
        stale = self.send(agent, "u-stale", "1")
        self.assertIn("tidak lagi tersedia", stale["response"])
        self.assertNotIn(foreign.name, stale["response"])
        self.assertEqual(service.update_calls, [])

    def test_update_workflow_round_trip_preserves_candidate_order(self):
        memory, _service, agent = self.update_agent([reservation(1), reservation(2)])
        self.send(agent, "u-codec", "ubah reservasi")
        snapshot = capture_reservation_workflow_snapshot_v2(memory, "u-codec")
        payload = snapshot.materialize()
        decoded = decode_workflow_snapshot_v2(payload).materialize()
        self.assertEqual(
            decoded["update_reservation_candidate_references"],
            [reference_for(2), reference_for(1)],
        )
        restored = MemoryManager()
        restored.replace_reservation_workflow_state("u-codec", decoded)
        self.assertEqual(
            restored.get_session("u-codec")["update_reservation_candidate_references"],
            [reference_for(2), reference_for(1)],
        )

    def test_candidate_codec_rejects_duplicates_overflow_and_bad_references(self):
        base = {
            "update_reservation_stage": "select_reservation_reference",
            "reservation_reference": None,
            "editing_field": None,
        }
        invalid_candidates = (
            [reference_for(1), reference_for(1)],
            [reference_for(index) for index in range(1, 7)],
            ["RSV_not-valid", reference_for(2)],
        )
        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates):
                with self.assertRaises(ConversationMemoryValidationError):
                    build_workflow_snapshot_v2(
                        {
                            **base,
                            "update_reservation_candidate_references": candidates,
                        }
                    )

    def test_reset_midway_discards_selection_and_restart_rebuilds_it(self):
        memory, _service, agent = self.update_agent([reservation(1), reservation(2)])
        self.send(agent, "u-reset", "ubah reservasi")
        memory.clear_session("u-reset")
        state = memory.get_session("u-reset")
        self.assertIsNone(state.get("update_reservation_stage"))
        restarted = self.send(agent, "u-reset", "ubah reservasi")
        self.assertIn("Pilih reservasi", restarted["response"])

    def test_cancel_zero_and_single_no_are_safe(self):
        memory, service, agent = self.cancel_agent([])
        zero = self.send(agent, "c-zero", "batalkan reservasi")
        self.assertEqual(zero["status"], "no_reservations")
        self.assertIsNone(memory.get_session("c-zero").get("cancel_reservation_stage"))

        memory, service, agent = self.cancel_agent([reservation(1)])
        start = self.send(agent, "c-no", "batalkan reservasi")
        self.assertNotIn("RSV_", start["response"])
        rejected = self.send(agent, "c-no", "Tidak")
        self.assertEqual(rejected["status"], "cancellation_rejected")
        self.assertEqual(service.cancel_calls, [])

    def test_cancel_single_selection_keeps_final_destructive_confirmation(self):
        row = reservation(1)
        memory, service, agent = self.cancel_agent([row])
        self.send(agent, "c-one", "batalkan reservasi")
        selection = self.send(agent, "c-one", "Ya")
        self.assertIn("Yakin ingin membatalkan", selection["response"])
        self.assertEqual(
            memory.get_session("c-one")["cancel_reservation_stage"],
            agent.CONFIRM_CANCELLATION,
        )
        invalid = self.send(agent, "c-one", "mungkin")
        self.assertEqual(invalid["status"], "awaiting_cancellation")
        self.assertEqual(service.cancel_calls, [])
        cancelled = self.send(agent, "c-one", "Ya")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(service.cancel_calls, [row.reference])

    def test_cancel_multiple_numeric_invalid_stale_and_owner_isolation(self):
        foreign = reservation(4, owner=OTHER_OWNER)
        memory, service, agent = self.cancel_agent(
            [reservation(1), reservation(2), reservation(3), foreign]
        )
        self.send(agent, "c-many", "batalkan reservasi")
        invalid = self.send(agent, "c-many", "0")
        self.assertTrue(invalid["invalid_input"])
        selected = self.send(agent, "c-many", "2")
        self.assertIn("Yakin ingin membatalkan", selected["response"])
        self.assertEqual(
            memory.get_session("c-many")["cancel_reservation_reference"],
            reference_for(2),
        )
        self.assertEqual(service.cancel_calls, [])

        memory, service, agent = self.cancel_agent([reservation(1), reservation(2), foreign])
        self.send(agent, "c-stale", "batalkan reservasi")
        service.rows[reference_for(2)].status = "cancelled"
        stale = self.send(agent, "c-stale", "1")
        self.assertIn("tidak lagi tersedia", stale["response"])
        self.assertNotIn(foreign.name, stale["response"])
        self.assertEqual(service.cancel_calls, [])

    def test_cancel_workflow_round_trip_preserves_candidate_order(self):
        memory, _service, agent = self.cancel_agent([reservation(1), reservation(2)])
        self.send(agent, "c-codec", "batalkan reservasi")
        snapshot = capture_reservation_workflow_snapshot_v2(memory, "c-codec")
        payload = snapshot.materialize()
        decoded = decode_workflow_snapshot_v2(payload).materialize()
        self.assertEqual(
            decoded["cancel_reservation_candidate_references"],
            [reference_for(2), reference_for(1)],
        )

    def test_intent_switching_clears_incompatible_selection_state(self):
        rows = [reservation(1), reservation(2)]
        service = SelectionService(rows)
        orchestrator = AgentOrchestrator()
        orchestrator.update_reservation_agent = UpdateReservationAgent(
            orchestrator.memory_manager,
            service,
        )
        orchestrator.cancel_reservation_agent = CancelReservationAgent(
            orchestrator.memory_manager,
            service,
        )
        session_id = "switch"
        self.send(orchestrator.update_reservation_agent, session_id, "ubah reservasi")
        result = asyncio.run(
            orchestrator._handle_authenticated(
                session_id,
                "batalkan reservasi saya",
                self.db,
                OWNER,
            )
        )
        state = orchestrator.memory_manager.get_session(session_id)
        self.assertIn("Pilih reservasi", result.reply)
        self.assertIsNone(state["update_reservation_stage"])
        self.assertEqual(state["update_reservation_candidate_references"], [])
        self.assertEqual(
            state["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_REFERENCE,
        )

        result = asyncio.run(
            orchestrator._handle_authenticated(
                session_id,
                "ubah reservasi saya",
                self.db,
                OWNER,
            )
        )
        state = orchestrator.memory_manager.get_session(session_id)
        self.assertIn("Pilih reservasi", result.reply)
        self.assertIsNone(state["cancel_reservation_stage"])
        self.assertEqual(state["cancel_reservation_candidate_references"], [])
        self.assertEqual(
            state["update_reservation_stage"],
            UpdateReservationAgent.SELECT_RESERVATION_REFERENCE,
        )


if __name__ == "__main__":
    unittest.main()
