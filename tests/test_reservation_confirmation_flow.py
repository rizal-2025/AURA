import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.agents.orchestrator import AgentOrchestrator
from app.agents.reservation_agent import ReservationAgent
from app.agents.result import ReservationOperationType
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_workflow_snapshot import (
    capture_reservation_workflow_snapshot_v2,
)
from app.services.reservation.dto import PersistedReservationDTO


SEEDED_CREATE_RESERVATION_ID = (2**30) + 104_729
FROZEN_NOW = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def frozen_clock():
    return FROZEN_NOW


def persisted(identifier, *, people=4):
    return PersistedReservationDTO(
        id=identifier,
        name="Rizal",
        people=people,
        date="2026-07-19",
        time="19:00",
        status="pending",
        reference=f"RSV_{identifier:032x}",
    )


class TestReservationConfirmationFlow(unittest.TestCase):
    OWNER_CUSTOMER_ID = uuid4()

    def _seed_confirmation_state(self, memory, session_id="s-edit"):
        memory.update_session(session_id, {
            "intent": "reservation",
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
            "asked_fields": ["name", "people", "date", "time"],
        })

    def _send_confirmation_message(
        self,
        agent,
        memory,
        session_id,
        message,
        *,
        db=None,
    ):
        return asyncio.run(
            agent.run(
                [{"action": "collect_missing_fields"}],
                memory.get_session(session_id),
                message,
                session_id=session_id,
                owner_customer_id=self.OWNER_CUSTOMER_ID,
                db=db,
            )
        )

    def test_confirmation_success_flow(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        state = {
            "intent": "reservation",
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
            "editing_field": None,
            "asked_fields": ["name", "people", "date", "time"],
        }
        memory.update_session("s1", state)
        before_snapshot = capture_reservation_workflow_snapshot_v2(
            memory,
            "s1",
        )

        db = MagicMock()
        with patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=persisted(SEEDED_CREATE_RESERVATION_ID),
        ) as create_reservation:
            payload = asyncio.run(
                agent.handle_confirmation(
                    "ya",
                    "s1",
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                    db=db,
                )
            )

        result = AgentOrchestrator._turn_result_from_agent_payload(payload)
        boundary_text = "\n".join(
            (
                result.reply,
                repr(result),
                repr(result.reservation_operation),
                str(vars(result.reservation_operation)),
                str(memory.get_session("s1")),
                str(before_snapshot.materialize()),
            )
        )
        self.assertNotIn(str(SEEDED_CREATE_RESERVATION_ID), boundary_text)
        self.assertIn("Reservasi berhasil dibuat", result.reply)
        self.assertNotIn("Nomor reservasi", result.reply)
        self.assertIn(
            persisted(SEEDED_CREATE_RESERVATION_ID).reference,
            result.reply,
        )
        self.assertEqual(
            result.reservation_operation.operation,
            ReservationOperationType.CREATED,
        )
        self.assertEqual(
            result.reservation_operation.reference,
            persisted(SEEDED_CREATE_RESERVATION_ID).reference,
        )
        self.assertEqual(memory.get_session("s1")["completed"], True)
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.OWNER_CUSTOMER_ID,
        )

    def test_create_with_missing_public_reference_fails_closed(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory, "missing-reference")
        unsafe = PersistedReservationDTO(
            id=987654,
            name="Rizal",
            people=4,
            date="2026-07-19",
            time="19:00",
            status="pending",
            reference=None,
        )
        agent.reservation_service.create_reservation = MagicMock(
            return_value=unsafe
        )

        result = asyncio.run(
            agent.handle_confirmation(
                "Ya",
                "missing-reference",
                owner_customer_id=self.OWNER_CUSTOMER_ID,
                db=MagicMock(),
            )
        )

        self.assertEqual(result["status"], "reference_unavailable")
        self.assertEqual(
            result["response"],
            "Data reservasi belum dapat diproses dengan aman. Silakan coba lagi nanti.",
        )
        self.assertNotIn("reservation_operation", result)
        self.assertNotIn("987654", result["response"])
        self.assertFalse(memory.get_session("missing-reference")["completed"])

    def test_phrase_aware_positive_confirmations_create_once(self):
        phrases = (
            "Ya lanjut",
            "Iya lanjut",
            "Iya benar",
            "Oke lanjut",
            "Oke gas",
            "Sip lanjut",
            "Betul lanjutkan",
            "Sudah benar",
            "Lanjutkan",
            "Silakan lanjutkan",
            "Boleh lanjut",
            "Setuju",
            "Sesuai",
            "Pas",
        )
        for index, phrase in enumerate(phrases, start=1):
            with self.subTest(phrase=phrase):
                memory = MemoryManager()
                session_id = f"positive-phrase-{index}"
                self._seed_confirmation_state(memory, session_id)
                agent = ReservationAgent(
                    memory_manager=memory,
                    clock=frozen_clock,
                )
                with patch.object(
                    agent.reservation_service,
                    "create_reservation",
                    return_value=persisted(100 + index),
                ) as create_reservation:
                    result = self._send_confirmation_message(
                        agent,
                        memory,
                        session_id,
                        phrase,
                        db=MagicMock(),
                    )

                self.assertEqual(result["status"], "completed")
                self.assertTrue(memory.get_session(session_id)["completed"])
                create_reservation.assert_called_once()

    def test_phrase_aware_rejections_never_create(self):
        phrases = (
            "Jangan lanjut",
            "Ga jadi",
            "Nga jadi",
            "Ngga jadi",
            "Nggak jadi",
            "Gak jadi",
            "Enggak jadi",
            "Ga usah",
            "Tidak usah",
            "Tidak jadi",
            "Batal aja",
            "Batalkan saja",
            "Batalkan reservasinya",
            "Batalin reservasinya",
            "Batalin reservasi",
            "Batalkan reservasi",
            "Tolong hapus pesanan meja",
            "Tolong cancel booking saya",
            "Sudah tidak perlu",
            "Jangan diproses",
            "Tidak jadi pesan",
            "Nggak jadi lanjut",
            "Iya tapi jangan lanjut",
        )
        for index, phrase in enumerate(phrases, start=1):
            with self.subTest(phrase=phrase):
                memory = MemoryManager()
                session_id = f"negative-phrase-{index}"
                self._seed_confirmation_state(memory, session_id)
                agent = ReservationAgent(
                    memory_manager=memory,
                    clock=frozen_clock,
                )
                with patch.object(
                    agent.reservation_service,
                    "create_reservation",
                ) as create_reservation:
                    result = self._send_confirmation_message(
                        agent,
                        memory,
                        session_id,
                        phrase,
                        db=MagicMock(),
                    )

                self.assertEqual(result["status"], "rejected")
                self.assertEqual(
                    result["response"],
                    "Baik, reservasi tidak dilanjutkan.",
                )
                session = memory.get_session(session_id)
                self.assertFalse(session["completed"])
                self.assertFalse(session.get("awaiting_confirmation", False))
                self.assertNotIn("field", result["response"].casefold())
                self.assertIsNone(
                    capture_reservation_workflow_snapshot_v2(memory, session_id)
                )
                create_reservation.assert_not_called()

    def test_rejection_clears_all_pending_create_state(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory, "s1")
        memory.update_session("s1", {"editing_field": None})

        result = asyncio.run(agent.handle_confirmation("nggak jadi", "s1"))
        session = memory.get_session("s1")

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["response"],
            "Baik, reservasi tidak dilanjutkan.",
        )
        self.assertIsNone(session["intent"])
        self.assertIsNone(session["name"])
        self.assertIsNone(session["people"])
        self.assertIsNone(session["date"])
        self.assertIsNone(session["time"])
        self.assertFalse(session["completed"])
        self.assertFalse(session.get("awaiting_confirmation", False))
        self.assertIsNone(session["editing_field"])
        self.assertNotIn("asked_fields", session)

    def test_rejection_calls_no_reservation_mutation_method(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory, "s1")

        with (
            patch.object(
                agent.reservation_service,
                "create_reservation",
            ) as create_reservation,
            patch.object(
                agent.reservation_service.repository,
                "create",
            ) as repository_create,
            patch.object(
                agent.reservation_service.repository,
                "update_reservation_field_by_public_reference",
            ) as repository_update,
            patch.object(
                agent.reservation_service.repository,
                "cancel_reservation_by_public_reference",
            ) as repository_cancel,
        ):
            asyncio.run(agent.handle_confirmation("batal aja", "s1"))

        create_reservation.assert_not_called()
        repository_create.assert_not_called()
        repository_update.assert_not_called()
        repository_cancel.assert_not_called()

    def test_new_create_after_rejection_starts_from_clean_state(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory, "s1")
        asyncio.run(agent.handle_confirmation("tidak usah", "s1"))

        result = self._send_confirmation_message(
            agent,
            memory,
            "s1",
            "Saya ingin membuat reservasi",
        )
        session = memory.get_session("s1")

        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["field"], "name")
        self.assertIsNone(session["name"])
        self.assertIsNone(session["people"])
        self.assertIsNone(session["date"])
        self.assertIsNone(session["time"])

    def test_memory_state_updates(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": False}
        memory.update_session("s1", state)

        db = MagicMock()
        with patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=persisted(42),
        ):
            asyncio.run(
                agent.handle_confirmation(
                    "ya",
                    "s1",
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                    db=db,
                )
            )
        state = memory.get_session("s1")

        self.assertEqual(state["completed"], True)
        self.assertEqual(state["awaiting_confirmation"], False)

    def test_orchestrator_accepts_confirmation_and_completes_reservation(self):
        orchestrator = AgentOrchestrator()
        orchestrator.workflow._agents[
            "reservation"
        ].reservation_service.clock = frozen_clock

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()
        orchestrator.memory_manager.update_session("s-confirm", {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
        })

        db = MagicMock()
        with patch(
            "app.agents.reservation_agent.ReservationService.create_reservation",
            return_value=persisted(43),
        ):
            result = asyncio.run(
                orchestrator.handle(
                    "s-confirm",
                    "ya",
                    db,
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                )
            )
        session = orchestrator.memory_manager.get_session("s-confirm")

        self.assertIn("Reservasi berhasil dibuat", result)
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        self.assertEqual(session["reservation_reference"], f"RSV_{43:032x}")
        self.assertNotIn("reservation_id", session)

    def test_orchestrator_rejection_terminates_without_handoff(self):
        orchestrator = AgentOrchestrator()
        orchestrator.workflow._agents[
            "reservation"
        ].reservation_service.clock = frozen_clock

        class DummyClassifier:
            async def classify(self, message):
                return {"intent": "reservation", "confidence": 0.95}

        class DummyAI:
            async def chat(self, message):
                return "fallback"

        orchestrator.intent_classifier = DummyClassifier()
        orchestrator.ai = DummyAI()
        orchestrator.memory_manager.update_session("s-reject", {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
        })

        with (
            patch.object(
                orchestrator.workflow._agents["reservation"].reservation_service,
                "create_reservation",
            ) as create_reservation,
            patch.object(orchestrator, "_create_handoff") as create_handoff,
        ):
            result = asyncio.run(
                orchestrator.handle(
                    "s-reject",
                    "tidak",
                    None,
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                ),
            )
        session = orchestrator.memory_manager.get_session("s-reject")

        self.assertEqual(result, "Baik, reservasi tidak dilanjutkan.")
        self.assertFalse(session.get("awaiting_confirmation", False))
        self.assertIsNone(session["intent"])
        self.assertIsNone(session["editing_field"])
        self.assertFalse(session.get("handoff_required", False))
        create_reservation.assert_not_called()
        create_handoff.assert_not_called()

    def test_edit_people(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        selection = self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Saya ingin mengubah jumlah orang",
        )

        self.assertEqual(memory.get_session("s-edit")["editing_field"], "people")
        self.assertTrue(memory.get_session("s-edit")["awaiting_confirmation"])
        self.assertEqual(selection["response"], "Baik, jumlah orang menjadi berapa?")

        result = self._send_confirmation_message(agent, memory, "s-edit", "7")

        session = memory.get_session("s-edit")
        self.assertEqual(session["people"], 7)
        self.assertIsNone(session["editing_field"])
        self.assertTrue(session["awaiting_confirmation"])
        self.assertIn("Jumlah: 7 orang", result["response"])

    def test_edit_name(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah nama")
        result = self._send_confirmation_message(agent, memory, "s-edit", "Budi")

        session = memory.get_session("s-edit")
        self.assertEqual(session["name"], "Budi")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Nama: Budi", result["response"])

    def test_batal_aja_remains_valid_while_explicitly_editing_name(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Saya ingin mengubah nama",
        )
        result = self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Batal Aja",
        )
        session = memory.get_session("s-edit")

        self.assertEqual(session["name"], "Batal Aja")
        self.assertIsNone(session["editing_field"])
        self.assertTrue(session["awaiting_confirmation"])
        self.assertIn("Nama: Batal Aja", result["response"])

    def test_natural_create_and_confirmation_edit_share_canonical_name(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)
        extracted = asyncio.run(
            agent.entity_extractor.extract(
                "Buat reservasi atas nama O\u2019Connor, untuk 4 orang"
            )
        )

        self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Saya ingin mengubah nama",
        )
        result = self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "O\u2019Connor!",
        )

        self.assertEqual(extracted["name"], "O\u2019Connor")
        self.assertEqual(memory.get_session("s-edit")["name"], extracted["name"])
        self.assertIn("Nama: O\u2019Connor", result["response"])

    def test_invalid_confirmation_name_keeps_edit_state(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)
        original_name = memory.get_session("s-edit")["name"]

        self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Saya ingin mengubah nama",
        )
        result = self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Bad/Name",
        )

        session = memory.get_session("s-edit")
        self.assertEqual(session["name"], original_name)
        self.assertEqual(session["editing_field"], "name")
        self.assertTrue(session["awaiting_confirmation"])
        self.assertEqual(result["response"], "Baik, nama menjadi siapa?")

    def test_edit_date(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah tanggal")
        result = self._send_confirmation_message(agent, memory, "s-edit", "2026-07-25")

        session = memory.get_session("s-edit")
        self.assertEqual(session["date"], "2026-07-25")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Tanggal: 25 Juli 2026", result["response"])

    def test_edit_time(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah jam")
        result = self._send_confirmation_message(agent, memory, "s-edit", "jam 8 malam")

        session = memory.get_session("s-edit")
        self.assertEqual(session["time"], "20:00")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Jam: 20.00", result["response"])

    def test_direct_edit(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        result = self._send_confirmation_message(
            agent,
            memory,
            "s-edit",
            "Ubah jumlah menjadi 7 orang",
        )

        session = memory.get_session("s-edit")
        self.assertEqual(session["people"], 7)
        self.assertIsNone(session["editing_field"])
        self.assertTrue(session["awaiting_confirmation"])
        self.assertIn("Jumlah: 7 orang", result["response"])
        self.assertNotIn("menjadi berapa", result["response"])

    def test_edit_then_confirm(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah jumlah orang")
        self._send_confirmation_message(agent, memory, "s-edit", "7")

        db = MagicMock()
        with patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=persisted(44, people=7),
        ) as create_reservation:
            result = self._send_confirmation_message(
                agent,
                memory,
                "s-edit",
                "ya",
                db=db,
            )

        saved_data = create_reservation.call_args.args[1]
        session = memory.get_session("s-edit")
        self.assertEqual(saved_data.people, 7)
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.OWNER_CUSTOMER_ID,
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        self.assertEqual(session["reservation_reference"], f"RSV_{44:032x}")
        self.assertNotIn("reservation_id", session)
        self.assertIs(create_reservation.call_args.args[0], db)

    def test_explicit_correction_then_confirm_saves_updated_value(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory, clock=frozen_clock)
        session_id = "s-explicit-edit"

        normal = self._send_confirmation_message(
            agent,
            memory,
            session_id,
            "Saya ingin reservasi besok jam 7 malam untuk 4 orang atas nama Rizal",
        )
        self.assertEqual(normal["status"], "awaiting_confirmation")

        selection = self._send_confirmation_message(
            agent,
            memory,
            session_id,
            "ubah jam",
        )
        self.assertEqual(memory.get_session(session_id)["editing_field"], "time")
        self.assertEqual(selection["response"], "Baik, jam menjadi berapa?")

        summary = self._send_confirmation_message(
            agent,
            memory,
            session_id,
            "jam 8 malam",
        )
        self.assertEqual(memory.get_session(session_id)["time"], "20:00")
        self.assertIn("Jam: 20.00", summary["response"])

        db = MagicMock()
        with patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=persisted(45),
        ) as create_reservation:
            confirmed = self._send_confirmation_message(
                agent,
                memory,
                session_id,
                "Ya",
                db=db,
            )

        saved_data = create_reservation.call_args.args[1]
        session = memory.get_session(session_id)
        self.assertEqual(saved_data.time, "20:00")
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.OWNER_CUSTOMER_ID,
        )
        self.assertEqual(confirmed["status"], "completed")
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        self.assertIs(create_reservation.call_args.args[0], db)


if __name__ == "__main__":
    unittest.main()
