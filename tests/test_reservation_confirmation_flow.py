import asyncio
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.agents.orchestrator import AgentOrchestrator
from app.agents.reservation_agent import ReservationAgent
from app.brain.memory_manager import MemoryManager


class TestReservationConfirmationFlow(unittest.TestCase):
    OWNER_CUSTOMER_ID = uuid4()

    def _seed_confirmation_state(self, memory, session_id="s-edit"):
        memory.update_session(session_id, {
            "name": "Rizal",
            "people": 4,
            "date": "2026-07-19",
            "time": "19:00",
            "completed": False,
            "awaiting_confirmation": True,
        })

    def _send_confirmation_message(self, agent, memory, session_id, message):
        return asyncio.run(
            agent.run(
                [{"action": "collect_missing_fields"}],
                memory.get_session(session_id),
                message,
                session_id=session_id,
                owner_customer_id=self.OWNER_CUSTOMER_ID,
            )
        )

    def test_confirmation_success_flow(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": False}
        memory.update_session("s1", state)

        with patch("app.agents.reservation_agent.SessionLocal", return_value=MagicMock()), patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=MagicMock(),
        ) as create_reservation:
            result = asyncio.run(
                agent.handle_confirmation(
                    "ya",
                    "s1",
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                )
            )

        self.assertIn("Reservasi berhasil dibuat", result["response"])
        self.assertEqual(memory.get_session("s1")["completed"], True)
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.OWNER_CUSTOMER_ID,
        )

    def test_rejection_flow(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": True}
        memory.update_session("s1", state)

        result = asyncio.run(agent.handle_confirmation("tidak", "s1"))

        self.assertIn("Silakan kirim data yang ingin diperbaiki", result["response"])
        self.assertTrue(memory.get_session("s1")["awaiting_confirmation"])
        self.assertIsNone(memory.get_session("s1")["editing_field"])

    def test_update_after_rejection(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": True}
        memory.update_session("s1", state)

        asyncio.run(agent.handle_confirmation("tidak", "s1"))
        memory.update_session("s1", {"time": "20:00"})
        state = memory.get_session("s1")

        self.assertEqual(state["time"], "20:00")

    def test_memory_state_updates(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        state = {"name": "Rizal", "people": 4, "date": "2026-07-19", "time": "19:00", "completed": False, "awaiting_confirmation": False}
        memory.update_session("s1", state)

        with patch("app.agents.reservation_agent.SessionLocal", return_value=MagicMock()), patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=MagicMock(),
        ):
            asyncio.run(
                agent.handle_confirmation(
                    "ya",
                    "s1",
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                )
            )
        state = memory.get_session("s1")

        self.assertEqual(state["completed"], True)
        self.assertEqual(state["awaiting_confirmation"], False)

    def test_orchestrator_accepts_confirmation_and_completes_reservation(self):
        orchestrator = AgentOrchestrator()

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

        with patch("app.agents.reservation_agent.SessionLocal", return_value=MagicMock()), patch(
            "app.agents.reservation_agent.ReservationService.create_reservation",
            return_value=MagicMock(),
        ):
            result = asyncio.run(
                orchestrator.handle(
                    "s-confirm",
                    "ya",
                    None,
                    owner_customer_id=self.OWNER_CUSTOMER_ID,
                )
            )
        session = orchestrator.memory_manager.get_session("s-confirm")

        self.assertIn("Reservasi berhasil dibuat", result)
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        self.assertIn("reservation_id", session)

    def test_orchestrator_keeps_confirmation_mode_after_rejection(self):
        orchestrator = AgentOrchestrator()

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

        result = asyncio.run(
            orchestrator.handle(
                "s-reject",
                "tidak",
                None,
                owner_customer_id=self.OWNER_CUSTOMER_ID,
            ),
        )
        session = orchestrator.memory_manager.get_session("s-reject")

        self.assertIn("field", result.lower())
        self.assertTrue(session["awaiting_confirmation"])
        self.assertIsNone(session["editing_field"])

    def test_edit_people(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
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
        agent = ReservationAgent(memory_manager=memory)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah nama")
        result = self._send_confirmation_message(agent, memory, "s-edit", "Budi")

        session = memory.get_session("s-edit")
        self.assertEqual(session["name"], "Budi")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Nama: Budi", result["response"])

    def test_natural_create_and_confirmation_edit_share_canonical_name(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
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
        agent = ReservationAgent(memory_manager=memory)
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
        agent = ReservationAgent(memory_manager=memory)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah tanggal")
        result = self._send_confirmation_message(agent, memory, "s-edit", "2026-07-25")

        session = memory.get_session("s-edit")
        self.assertEqual(session["date"], "2026-07-25")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Tanggal: 2026-07-25", result["response"])

    def test_edit_time(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah jam")
        result = self._send_confirmation_message(agent, memory, "s-edit", "jam 8 malam")

        session = memory.get_session("s-edit")
        self.assertEqual(session["time"], "20:00")
        self.assertIsNone(session["editing_field"])
        self.assertIn("Jam: 20:00", result["response"])

    def test_direct_edit(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
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
        agent = ReservationAgent(memory_manager=memory)
        self._seed_confirmation_state(memory)

        self._send_confirmation_message(agent, memory, "s-edit", "Saya ingin mengubah jumlah orang")
        self._send_confirmation_message(agent, memory, "s-edit", "7")

        db = MagicMock()
        with patch("app.agents.reservation_agent.SessionLocal", return_value=db), patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=MagicMock(),
        ) as create_reservation:
            result = self._send_confirmation_message(agent, memory, "s-edit", "ya")

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
        self.assertIn("reservation_id", session)
        db.close.assert_called_once()

    def test_rejection_edit_people_then_confirm_saves_updated_value(self):
        memory = MemoryManager()
        agent = ReservationAgent(memory_manager=memory)
        session_id = "s-reject-edit"

        normal = self._send_confirmation_message(
            agent,
            memory,
            session_id,
            "Saya ingin reservasi besok jam 7 malam untuk 4 orang atas nama Rizal",
        )
        self.assertEqual(normal["status"], "awaiting_confirmation")

        rejected = self._send_confirmation_message(agent, memory, session_id, "Tidak")
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(memory.get_session(session_id)["awaiting_confirmation"])

        selection = self._send_confirmation_message(
            agent,
            memory,
            session_id,
            "Saya ingin mengubah jumlah orang",
        )
        self.assertEqual(memory.get_session(session_id)["editing_field"], "people")
        self.assertEqual(selection["response"], "Baik, jumlah orang menjadi berapa?")

        summary = self._send_confirmation_message(agent, memory, session_id, "7")
        self.assertEqual(memory.get_session(session_id)["people"], 7)
        self.assertIn("Jumlah: 7 orang", summary["response"])

        db = MagicMock()
        with patch("app.agents.reservation_agent.SessionLocal", return_value=db), patch.object(
            agent.reservation_service,
            "create_reservation",
            return_value=MagicMock(),
        ) as create_reservation:
            confirmed = self._send_confirmation_message(agent, memory, session_id, "Ya")

        saved_data = create_reservation.call_args.args[1]
        session = memory.get_session(session_id)
        self.assertEqual(saved_data.people, 7)
        self.assertEqual(
            create_reservation.call_args.kwargs["owner_customer_id"],
            self.OWNER_CUSTOMER_ID,
        )
        self.assertEqual(confirmed["status"], "completed")
        self.assertTrue(session["completed"])
        self.assertFalse(session["awaiting_confirmation"])
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
