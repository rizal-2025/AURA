"""Offline state/DB oracles with a fresh agent and durable restore each turn."""

import asyncio
from datetime import datetime, timezone, date
import os
import json
from pathlib import Path
import random
import unittest
from uuid import uuid4

from sqlalchemy import select
from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.core.locale import SupportedLocale, presentation_locale
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.services.conversation_workflow_state_service import ConversationWorkflowStateService
from app.services.reservation.service import ReservationService
from app.schemas.reservation import ReservationCreate
from app.utils.datetime_parser import DatetimeParser
from tests import test_persisted_reservation_update as fixture

NOW = datetime(2026, 9, 5, 9, 11, tzinfo=timezone.utc)  # 16:11 Jakarta
TIME_ORACLE = (
    ("8 pagi", "08:00"), ("10 pagi", "10:00"), ("11 siang", "11:00"),
    ("11.30 siang", "11:30"), ("12 siang", "12:00"), ("1 siang", "13:00"),
    ("2 siang", "14:00"), ("3 sore", "15:00"), ("7 malam", "19:00"),
    ("11 malam", "23:00"), ("12 malam", "00:00"), ("11 am", "11:00"),
    ("11 pm", "23:00"), ("12 am", "00:00"), ("12 pm", "12:00"),
    ("00:00", "00:00"), ("23:59", "23:59"), ("24:00", None),
    ("25:00", None), ("12:60", None), ("11:30 pm", "23:30"),
    ("12:30 am", "00:30"), ("1.30 siang", "13:30"), ("7 siang", None),
)


class GeneratedParserTests(unittest.TestCase):
    def test_generated_time_and_calendar_oracles(self):
        cases = {}
        for phrase, expected in TIME_ORACLE:
            for prefix in ("", "jam ", "pukul "):
                for casing in (str.lower, str.upper, str.title):
                    for suffix in ("", "!"):
                        cases[casing(prefix + phrase) + suffix] = expected
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(DatetimeParser.parse_time(phrase), expected)
        for year in (2024, 2026, 2028):
            for month in range(1, 13):
                for day in (28, 29, 30, 31):
                    phrase = f"{year}-{month:02d}-{day:02d}"
                    try:
                        expected = date(year, month, day).isoformat()
                    except ValueError:
                        expected = None
                    with self.subTest(phrase=phrase):
                        self.assertEqual(DatetimeParser.parse_date(phrase, reference_date=date(2026, 9, 5)), expected)


class DialogueContract:
    """Shared SQLite/real PostgreSQL campaign. No provider implementation used."""

    def new_scope(self, *, create=False, now=NOW):
        self.now = now
        self.key = "simulation-" + uuid4().hex
        with self.Session() as db:
            owner = Customer()
            db.add(owner)
            db.commit()
            self.owner_id = owner.id
            self.reference = None
            if not create:
                row = ReservationService(clock=lambda: self.now).create_reservation(
                    db, ReservationCreate(name="Jessica", people=8, date="2026-09-05", time="20:00"),
                    owner_customer_id=self.owner_id,
                )
                self.reference = row.reference

    def seed(self, state):
        with self.Session() as db:
            memory = MemoryManager()
            workflow = ConversationWorkflowStateService(memory)
            workflow.restore(db, owner_customer_id=self.owner_id, memory_key=self.key)
            memory.replace_reservation_workflow_state(self.key, state)
            workflow.publish(db, owner_customer_id=self.owner_id, memory_key=self.key)

    def update_prompt(self, field):
        self.seed({"update_reservation_stage": "input_value", "reservation_reference": self.reference,
                   "editing_field": field})

    def create_prompt(self, **fields):
        if fields.get("asked_fields"):
            order = ["name", "people", "date", "time"]
            fields["asked_fields"] = order[:order.index(fields["asked_fields"][-1]) + 1]
        self.seed({"intent": "reservation", "name": None, "people": None, "date": None, "time": None,
                   "completed": False, "awaiting_confirmation": False, "editing_field": None,
                   "asked_fields": [], **fields})

    def rows(self):
        with self.Session() as db:
            return [(r.public_reference, r.name, r.people, r.date, r.time, r.status)
                    for r in db.scalars(select(Reservation).where(Reservation.owner_customer_id == self.owner_id))]

    def turn(self, message, *, create=False, locale=SupportedLocale.ID_ID):
        before = self.rows()
        with self.Session() as db, presentation_locale(locale):
            memory = MemoryManager()
            workflow = ConversationWorkflowStateService(memory)
            workflow.restore(db, owner_customer_id=self.owner_id, memory_key=self.key)
            if create:
                agent = ReservationAgent(memory_manager=memory, workflow_state_service=workflow, clock=lambda: self.now)
                result = asyncio.run(agent.run([{"action": "collect_missing_fields"}], memory.get_session(self.key),
                    message, self.key, self.owner_id, db))
            else:
                agent = UpdateReservationAgent(memory_manager=memory, workflow_state_service=workflow,
                                               reservation_service=ReservationService(clock=lambda: self.now), clock=lambda: self.now)
                result = asyncio.run(agent.run(db, self.key, message, self.owner_id))
            workflow.publish(db, owner_customer_id=self.owner_id, memory_key=self.key)
        # Independently reload the durable state, not the just-used agent dict.
        with self.Session() as db:
            memory = MemoryManager()
            ConversationWorkflowStateService(memory).restore(db, owner_customer_id=self.owner_id, memory_key=self.key)
            self.state = memory.get_session(self.key)
        evidence = os.environ.get("AURA_SIMULATION_EVIDENCE")
        if evidence:
            record = {"scenario_id": self.id(), "scope": self.key, "category": "create" if create else "update",
                      "locale": locale.value, "seed": os.environ.get("AURA_SIMULATION_SEED"),
                      "frozen_clock": self.now.isoformat(), "turn": message, "db_before": before,
                      "db_after": self.rows(), "persisted_draft": self.state,
                      "observed_status": result["status"], "response": result["response"],
                      "provider_call_contract": 0}
            with Path(evidence).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    def test_jessica_create_update_recovery(self):
        self.new_scope(create=True)
        self.create_prompt(name="Jessica", people=8, date="2026-09-05", asked_fields=["time"])
        self.assertTrue(self.turn("7 pagi", create=True)["invalid_input"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("8 malam", create=True)["status"], "awaiting_confirmation")
        self.assertEqual(self.state["time"], "20:00")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("ya", create=True)["status"], "completed")
        self.reference = self.rows()[0][0]
        self.update_prompt("time")
        before = self.rows()
        rejected = self.turn("11 siang")
        self.assertTrue(rejected["invalid_input"])
        self.assertNotIn("reservation_operation", rejected)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.turn("11 malam")["status"], "updated")
        self.assertEqual(self.rows()[0][1:5], ("Jessica", 8, "2026-09-05", "23:00"))

    def test_eleven_noon_is_valid_at_ten_for_create_update_and_direct(self):
        self.new_scope(create=True, now=datetime(2026, 9, 5, 3, tzinfo=timezone.utc))
        self.create_prompt(name="Jessica", people=8, date="2026-09-05", asked_fields=["time"])
        self.assertEqual(self.turn("11 siang", create=True)["status"], "awaiting_confirmation")
        self.assertEqual(self.state["time"], "11:00")
        self.assertEqual(self.turn("ya", create=True)["status"], "completed")
        self.reference = self.rows()[0][0]
        self.update_prompt("time")
        self.assertEqual(self.turn("11 siang")["status"], "updated")
        with self.Session() as db:
            value = ReservationService(clock=lambda: self.now).update_reservation_field_by_reference(
                db, self.reference, "time", "11:00", self.owner_id)
        self.assertEqual(value.time, "11:00")

    def test_partial_create_calendar_correction_and_rejection(self):
        self.new_scope(create=True)
        self.create_prompt(name="Dani Saputra", people=5, time="20:00", asked_fields=["date"])
        self.turn("Tanggal 31", create=True)
        self.assertEqual(self.state["pending_reservation_day"], 31)
        self.turn("Februari 2028", create=True)
        self.assertIsNone(self.state.get("date"))
        self.assertEqual(self.rows(), [])
        self.turn("Tanggal 5", create=True)
        self.assertEqual(self.state["pending_reservation_day"], 5)
        self.assertEqual(self.turn("September 2026", create=True)["status"], "awaiting_confirmation")
        self.assertEqual(self.state["date"], "2026-09-05")
        self.assertNotIn("pending_reservation_day", self.state)
        self.assertEqual(self.turn("tidak", create=True)["status"], "rejected")
        self.assertEqual(self.rows(), [])

    def test_inline_partial_confirmation_edit_requires_new_confirmation(self):
        self.new_scope(create=True)
        self.create_prompt(name="Jessica", people=8, date="2026-09-06", time="20:00", awaiting_confirmation=True)
        self.turn("ubah tanggal 5", create=True)
        self.assertEqual(self.state["pending_reservation_day"], 5)
        self.turn("ya", create=True)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("September 2026", create=True)["status"], "awaiting_confirmation")
        self.assertEqual(self.state["date"], "2026-09-05")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("ya", create=True)["status"], "completed")

    def test_curated_field_recovery_matrix(self):
        # 64 distinct input/field/locale contracts; every turn checks DB state.
        for index in range(16):
            for field in ("name", "people", "date", "time"):
                with self.subTest(scenario=f"curated-{field}-{index:02}"):
                    self.new_scope()
                    self.update_prompt(field)
                    before = self.rows()
                    invalid = {"name": "@@@", "people": "0", "date": "4 September 2026", "time": "11 siang"}[field]
                    result = self.turn(invalid)
                    self.assertTrue(result.get("invalid_input"))
                    self.assertEqual(self.rows(), before)
                    self.assertNotIn("reservation_operation", result)
                    value = {"name": "Nama " + chr(65 + index), "people": str(index + 1),
                             "date": f"{index + 6} September 2026", "time": f"23:{index:02}"}[field]
                    result = self.turn(value, locale=SupportedLocale.EN_US if index % 2 else SupportedLocale.ID_ID)
                    self.assertEqual(result["status"], "updated")
                    current = self.rows()[0]
                    target = {"name": 1, "people": 2, "date": 3, "time": 4}[field]
                    expected = {"name": value, "people": index + 1,
                                "date": f"2026-09-{index + 6:02}", "time": value}[field]
                    self.assertEqual(current[target], expected)
                    for column in range(6):
                        if column != target:
                            self.assertEqual(current[column], before[0][column])
                    ReservationService(clock=lambda: self.now).validate_new_reservation_datetime(current[3], current[4])

    def test_seeded_stateful_traces(self):
        seed = int(os.environ.get("AURA_SIMULATION_SEED", "20260905"))
        rng = random.Random(seed)
        seen = set()
        for trace in range(100):
            with self.subTest(seed=seed, trace=trace):
                self.new_scope()
                self.update_prompt("date")
                # Distinct calendar values give each trace a distinct oracle;
                # shuffle and branch choices vary the sequence across sweeps.
                from datetime import timedelta
                final_date = date(2026, 9, 6) + timedelta(days=trace)
                day = final_date.day
                month_year = final_date.strftime("%B %Y")
                seen.add(final_date.isoformat())
                self.turn(f"Tanggal {day}")
                self.assertEqual(self.state["pending_reservation_day"], day)
                before = self.rows()
                self.turn("terima kasih")
                self.assertEqual(self.rows(), before)
                self.assertEqual(self.state["pending_reservation_day"], day)
                if rng.choice((True, False)):
                    # Same production memory replacement contract used by reset.
                    self.seed({})
                    self.update_prompt("date")
                    self.turn(month_year)
                    self.assertEqual(self.rows(), before)
                    self.assertNotIn("pending_reservation_day", self.state)
                    self.turn(f"Tanggal {day}")
                self.assertEqual(self.turn(month_year)["status"], "updated")
                self.assertEqual(self.rows()[0][3], final_date.isoformat())
                self.assertNotIn("pending_reservation_day", self.state)
        self.assertEqual(len(seen), 100)


class LocalDialogueTests(DialogueContract, unittest.TestCase):
    setUp = fixture.PersistedReservationUpdateTests.setUp
