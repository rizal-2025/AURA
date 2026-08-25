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

    def list_selectable_reservation_page(
        self,
        _db,
        owner_customer_id,
        *,
        after_public_reference=None,
        page_size=5,
    ):
        rows = sorted(
            (
                row
                for row in self.rows.values()
                if row.owner_customer_id == owner_customer_id
                and row.status.lower() != "cancelled"
            ),
            key=lambda row: row.id,
            reverse=True,
        )
        if after_public_reference is not None:
            cursor = self.rows.get(after_public_reference)
            if cursor is None or cursor.owner_customer_id != owner_customer_id:
                rows = []
            else:
                rows = [row for row in rows if row.id < cursor.id]
        return SimpleNamespace(
            reservations=tuple(rows[:page_size]),
            has_more=len(rows) > page_size,
        )

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
        self.assertIn("10 Agustus 2026", start["response"])
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

    def test_update_five_reservations_remain_one_bounded_page(self):
        memory, _service, agent = self.update_agent(
            [reservation(index) for index in range(1, 6)]
        )
        start = self.send(agent, "u-five", "ubah reservasi")
        state = memory.get_session("u-five")
        self.assertEqual(
            state["update_reservation_candidate_references"],
            [reference_for(index) for index in range(5, 0, -1)],
        )
        self.assertFalse(state["update_reservation_page_has_more"])
        self.assertNotIn("berikutnya", start["response"])

    def test_update_sixth_reservation_is_reachable_and_updated_normally(self):
        rows = [reservation(index) for index in range(1, 7)]
        memory, service, agent = self.update_agent(rows)
        start = self.send(agent, "u-six", "ubah reservasi")
        self.assertIn("berikutnya", start["response"])
        self.assertEqual(
            memory.get_session("u-six")["update_reservation_candidate_references"],
            [reference_for(index) for index in range(6, 1, -1)],
        )

        later = self.send(agent, "u-six", "berikutnya")
        self.assertIn('"awal"', later["response"])
        self.assertEqual(
            memory.get_session("u-six")["update_reservation_candidate_references"],
            [reference_for(1)],
        )
        self.send(agent, "u-six", "1")
        self.send(agent, "u-six", "people")
        updated = self.send(agent, "u-six", "12")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(
            service.update_calls,
            [(reference_for(1), "people", 12)],
        )

    def test_update_ten_reservations_are_all_reachable_across_pages(self):
        rows = [reservation(index) for index in range(1, 11)]
        memory, service, agent = self.update_agent(rows)
        self.send(agent, "u-ten", "ubah reservasi")
        first_page = list(
            memory.get_session("u-ten")["update_reservation_candidate_references"]
        )
        self.send(agent, "u-ten", "berikutnya")
        second_page = list(
            memory.get_session("u-ten")["update_reservation_candidate_references"]
        )
        self.assertEqual(
            first_page + second_page,
            [reference_for(index) for index in range(10, 0, -1)],
        )
        self.send(agent, "u-ten", "5")
        repeated = self.send(agent, "u-ten", "5")
        self.assertTrue(repeated["invalid_input"])
        self.assertEqual(service.update_calls, [])
        self.send(agent, "u-ten", "people")
        self.send(agent, "u-ten", "11")
        self.assertEqual(
            service.update_calls,
            [(reference_for(1), "people", 11)],
        )

    def test_update_page_navigation_restart_stale_owner_and_reset_are_safe(self):
        foreign = reservation(7, owner=OTHER_OWNER)
        rows = [reservation(index) for index in range(1, 7)] + [foreign]
        memory, service, agent = self.update_agent(rows)
        self.send(agent, "u-nav", "ubah reservasi")
        self.send(agent, "u-nav", "berikutnya")
        later_state = memory.get_session("u-nav")
        self.assertEqual(
            later_state["update_reservation_candidate_references"],
            [reference_for(1)],
        )
        self.assertNotIn(foreign.reference, later_state["update_reservation_candidate_references"])
        invalid = self.send(agent, "u-nav", "lanjut saja")
        self.assertTrue(invalid["invalid_input"])
        self.assertEqual(
            memory.get_session("u-nav")["update_reservation_candidate_references"],
            [reference_for(1)],
        )

        snapshot = capture_reservation_workflow_snapshot_v2(memory, "u-nav")
        restored_memory = MemoryManager()
        restored_memory.replace_reservation_workflow_state(
            "u-restored",
            decode_workflow_snapshot_v2(snapshot.materialize()).materialize(),
        )
        restored_agent = UpdateReservationAgent(restored_memory, service)
        self.send(restored_agent, "u-restored", "1")
        self.assertEqual(
            restored_memory.get_session("u-restored")["reservation_reference"],
            reference_for(1),
        )

        returned = self.send(agent, "u-nav", "awal")
        self.assertIn("berikutnya", returned["response"])
        self.assertEqual(
            memory.get_session("u-nav")["update_reservation_candidate_references"],
            [reference_for(index) for index in range(6, 1, -1)],
        )
        self.send(agent, "u-nav", "berikutnya")
        no_next = self.send(agent, "u-nav", "berikutnya")
        self.assertIn("Tidak ada reservasi berikutnya", no_next["response"])
        self.assertEqual(
            memory.get_session("u-nav")["update_reservation_candidate_references"],
            [reference_for(1)],
        )

        service.rows[reference_for(1)].status = "cancelled"
        stale = self.send(agent, "u-nav", "1")
        self.assertIn("tidak lagi tersedia", stale["response"])
        self.assertEqual(service.update_calls, [])

        service.rows[reference_for(1)].status = "pending"
        memory.clear_session("u-nav")
        reset = self.send(agent, "u-nav", "ubah reservasi")
        self.assertIn("berikutnya", reset["response"])
        self.assertIsNone(memory.get_session("u-nav")["update_reservation_page_cursor"])

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

    def test_pre_pagination_selection_snapshot_refreshes_before_input_mapping(self):
        legacy_payload = {
            "update_reservation_stage": "select_reservation_reference",
            "reservation_reference": None,
            "editing_field": None,
            "update_reservation_candidate_references": [
                reference_for(2),
                reference_for(1),
            ],
        }
        memory = MemoryManager()
        memory.replace_reservation_workflow_state(
            "u-pre-pagination",
            decode_workflow_snapshot_v2(legacy_payload).materialize(),
        )
        service = SelectionService(
            [reservation(index) for index in range(1, 7)]
        )
        agent = UpdateReservationAgent(memory, service)
        refreshed = self.send(agent, "u-pre-pagination", "2")
        state = memory.get_session("u-pre-pagination")
        self.assertIn("berikutnya", refreshed["response"])
        self.assertIsNone(state["reservation_reference"])
        self.assertEqual(
            state["update_reservation_candidate_references"],
            [reference_for(index) for index in range(6, 1, -1)],
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

    def test_candidate_codec_rejects_malformed_pagination_state(self):
        candidates = [reference_for(index) for index in range(6, 1, -1)]
        base = {
            "update_reservation_stage": "select_reservation_reference",
            "reservation_reference": None,
            "editing_field": None,
            "update_reservation_candidate_references": candidates,
            "update_reservation_page_cursor": None,
            "update_reservation_page_has_more": True,
        }
        invalid = (
            {**base, "update_reservation_page_has_more": "yes"},
            {**base, "update_reservation_page_cursor": "RSV_invalid"},
            {
                **base,
                "update_reservation_page_cursor": candidates[0],
            },
            {
                **base,
                "update_reservation_candidate_references": candidates[:2],
            },
            {
                **base,
                "update_reservation_candidate_references": [],
                "update_reservation_page_has_more": False,
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    build_workflow_snapshot_v2(payload)

        cancel_base = {
            "cancel_reservation_stage": "select_reservation_reference",
            "cancel_reservation_reference": None,
            "cancel_reservation_candidate_references": candidates,
            "cancel_reservation_page_cursor": None,
            "cancel_reservation_page_has_more": True,
        }
        for payload in (
            {**cancel_base, "cancel_reservation_page_has_more": 1},
            {**cancel_base, "cancel_reservation_page_cursor": candidates[0]},
            {
                **cancel_base,
                "cancel_reservation_candidate_references": candidates[:3],
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ConversationMemoryValidationError):
                    build_workflow_snapshot_v2(payload)

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

    def test_cancel_five_six_and_ten_are_bounded_and_reachable(self):
        memory, _service, agent = self.cancel_agent(
            [reservation(index) for index in range(1, 6)]
        )
        five = self.send(agent, "c-five", "batalkan reservasi")
        self.assertNotIn("berikutnya", five["response"])
        self.assertEqual(
            len(memory.get_session("c-five")["cancel_reservation_candidate_references"]),
            5,
        )

        for count, choice, expected in ((6, "1", 1), (10, "5", 1)):
            with self.subTest(count=count):
                rows = [reservation(index) for index in range(1, count + 1)]
                memory, service, agent = self.cancel_agent(rows)
                session_id = f"c-{count}"
                start = self.send(agent, session_id, "batalkan reservasi")
                self.assertIn("berikutnya", start["response"])
                first_page = list(
                    memory.get_session(session_id)[
                        "cancel_reservation_candidate_references"
                    ]
                )
                self.send(agent, session_id, "berikutnya")
                second_page = list(
                    memory.get_session(session_id)[
                        "cancel_reservation_candidate_references"
                    ]
                )
                self.assertEqual(
                    first_page + second_page,
                    [reference_for(index) for index in range(count, 0, -1)],
                )
                selected = self.send(agent, session_id, choice)
                self.assertIn("Yakin ingin membatalkan", selected["response"])
                self.assertEqual(service.cancel_calls, [])
                repeated = self.send(agent, session_id, choice)
                self.assertIn("Yakin ingin membatalkan", repeated["response"])
                self.assertEqual(service.cancel_calls, [])
                cancelled = self.send(agent, session_id, "Ya")
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(service.cancel_calls, [reference_for(expected)])

    def test_cancel_later_page_restart_stale_owner_return_and_reset_are_safe(self):
        foreign = reservation(7, owner=OTHER_OWNER)
        rows = [reservation(index) for index in range(1, 7)] + [foreign]
        memory, service, agent = self.cancel_agent(rows)
        self.send(agent, "c-nav", "batalkan reservasi")
        self.send(agent, "c-nav", "berikutnya")
        state = memory.get_session("c-nav")
        self.assertEqual(
            state["cancel_reservation_candidate_references"],
            [reference_for(1)],
        )
        self.assertNotIn(foreign.reference, state["cancel_reservation_candidate_references"])

        snapshot = capture_reservation_workflow_snapshot_v2(memory, "c-nav")
        restored_memory = MemoryManager()
        restored_memory.replace_reservation_workflow_state(
            "c-restored",
            decode_workflow_snapshot_v2(snapshot.materialize()).materialize(),
        )
        restored_agent = CancelReservationAgent(restored_memory, service)
        self.send(restored_agent, "c-restored", "1")
        self.assertEqual(
            restored_memory.get_session("c-restored")["cancel_reservation_reference"],
            reference_for(1),
        )

        returned = self.send(agent, "c-nav", "awal")
        self.assertIn("berikutnya", returned["response"])
        self.send(agent, "c-nav", "berikutnya")
        service.rows[reference_for(1)].status = "cancelled"
        stale = self.send(agent, "c-nav", "1")
        self.assertIn("tidak lagi tersedia", stale["response"])
        self.assertEqual(service.cancel_calls, [])

        service.rows[reference_for(1)].status = "pending"
        memory.clear_session("c-nav")
        reset = self.send(agent, "c-nav", "batalkan reservasi")
        self.assertIn("berikutnya", reset["response"])
        self.assertIsNone(memory.get_session("c-nav")["cancel_reservation_page_cursor"])

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
        rows = [reservation(index) for index in range(1, 7)]
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
        self.send(orchestrator.update_reservation_agent, session_id, "berikutnya")
        result = asyncio.run(
            orchestrator._handle_authenticated(
                session_id,
                "batalkan reservasi saya",
                self.db,
                OWNER,
            )
        )
        state = orchestrator.memory_manager.get_session(session_id)
        self.assertIn("berikutnya", result.reply)
        self.assertIsNone(state["update_reservation_stage"])
        self.assertEqual(state["update_reservation_candidate_references"], [])
        self.assertIsNone(state["update_reservation_page_cursor"])
        self.assertIsNone(state["update_reservation_page_has_more"])
        self.assertEqual(
            state["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_REFERENCE,
        )
        self.send(orchestrator.cancel_reservation_agent, session_id, "berikutnya")

        result = asyncio.run(
            orchestrator._handle_authenticated(
                session_id,
                "ubah reservasi saya",
                self.db,
                OWNER,
            )
        )
        state = orchestrator.memory_manager.get_session(session_id)
        self.assertIn("berikutnya", result.reply)
        self.assertIsNone(state["cancel_reservation_stage"])
        self.assertEqual(state["cancel_reservation_candidate_references"], [])
        self.assertIsNone(state["cancel_reservation_page_cursor"])
        self.assertIsNone(state["cancel_reservation_page_has_more"])
        self.assertEqual(
            state["update_reservation_stage"],
            UpdateReservationAgent.SELECT_RESERVATION_REFERENCE,
        )


if __name__ == "__main__":
    unittest.main()
