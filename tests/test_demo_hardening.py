"""Deterministic dialogue hardening contracts; no live provider requests."""

import unittest
from datetime import date
import asyncio
from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.services.conversation_workflow_state_service import ConversationWorkflowStateService
from tests.test_persisted_reservation_update import PersistedReservationUpdateTests, NOW

from app.utils.datetime_parser import DatetimeParser


class TimeSemanticContractTests(unittest.TestCase):
    def test_noon_and_qualified_minutes(self):
        for text, expected in (("11 siang", "11:00"), ("11.30 siang", "11:30"),
                               ("11:30 pm", "23:30"), ("12:30 am", "00:30"),
                               ("1.30 siang", "13:30")):
            with self.subTest(text=text):
                self.assertEqual(DatetimeParser.parse_time(text), expected)

    def test_year_inference_remains_a_parser_policy_not_guard_bypass(self):
        self.assertEqual(DatetimeParser.parse_date("4 September", reference_date=date(2026, 9, 5)), "2027-09-04")

    def test_invalid_clock_cannot_fall_back_to_a_different_hour(self):
        for text in ("jam 8:60 malam", "jam 25:00", "12:60 pm", "25:00 malam"):
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_time(text))

    def test_conflicting_times_require_clarification(self):
        for text in ("8 pagi 9 malam", "11 siang atau 11 malam", "08:00 atau 20:00"):
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_time(text))

    def test_malformed_year_and_conflicting_date_are_not_inferred(self):
        for text in ("4 September 20266", "besok 4 September 2026"):
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))

    def test_mixed_format_conflicts_and_malformed_minutes(self):
        for text in ("11:30 atau 8 malam", "8.6 malam"):
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_time(text))
        for text in ("Senin 4 September 2026", "besok 2026-09-04", "2026-09-06 atau 2026-09-07"):
            with self.subTest(text=text):
                self.assertIsNone(DatetimeParser.parse_date(text, reference_date=date(2026, 9, 5)))
        self.assertEqual(DatetimeParser.parse_date("5 September 8 malam", reference_date=date(2026, 9, 5)), "2026-09-05")

    def test_confirmation_edit_cannot_revive_a_rejected_parse(self):
        agent = ReservationAgent(clock=lambda: NOW)
        for field, text in (("time", "ubah jam 11:30 atau 8 malam"),
                            ("date", "ubah tanggal 2026-09-06 atau 2026-09-07")):
            with self.subTest(field=field):
                self.assertIsNone(asyncio.run(agent._extract_direct_edit_value(field, text)))


class DateDialogueContractTests(PersistedReservationUpdateTests):
    # Reuse real workflow/repository fixture, not a mocked persistence boundary.
    def restart(self):
        self.workflow.publish(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        self.memory = MemoryManager()
        self.workflow = ConversationWorkflowStateService(self.memory)
        self.workflow.restore(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        self.agent = UpdateReservationAgent(memory_manager=self.memory, reservation_service=self.service,
                                           workflow_state_service=self.workflow, clock=lambda: NOW)

    def test_inferred_next_year_requires_explicit_input_before_update(self):
        self.prepare_update("date")
        result = self.send_update("4 September")
        self.assertEqual(result["status"], "awaiting_update")
        self.assertIn("2027", result["response"])
        self.update_spy.assert_not_called()
        self.assertEqual(self.reservation_fields()[2:], ("2026-09-05", "12:57"))
        self.restart()
        self.assertEqual(self.send_update("4 September 2027")["status"], "updated")

    def test_partial_day_survives_new_agent_request(self):
        self.prepare_update("date")
        self.assertEqual(self.send_update("Tanggal 5")["status"], "awaiting_update")
        self.assertEqual(self.durable_workflow()[2].get("pending_reservation_day"), None)
        self.restart()
        self.assertEqual(self.memory.get_session(self.key).get("pending_reservation_day"), 5)
        self.assertEqual(self.send_update("September 2026")["status"], "updated")
        self.assertEqual(self.reservation_fields()[2:], ("2026-09-05", "12:57"))
        self.assertNotIn("pending_reservation_day", self.memory.get_session(self.key))

    def test_unrelated_year_does_not_authorize_inferred_update(self):
        self.prepare_update("date")
        self.assertEqual(self.send_update("4 September, catatan 2026")["status"], "awaiting_update")
        self.update_spy.assert_not_called()

    def test_field_selection_can_carry_the_partial_day(self):
        self.prepare_update("date")
        state = self.memory.get_session(self.key)
        state.update(update_reservation_stage="select_field", editing_field=None)
        self.send_update("Tanggal 5")
        self.restart()
        self.assertEqual(self.memory.get_session(self.key).get("pending_reservation_day"), 5)
        self.assertEqual(self.send_update("September 2026")["status"], "updated")
