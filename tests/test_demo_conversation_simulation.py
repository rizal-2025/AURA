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

    def test_blocker_invalid_dates_never_mutate_and_recover(self):
        from tests.test_demo_blocker_fix import AMBIGUOUS_DATES, MALFORMED_DATES
        for text in AMBIGUOUS_DATES + MALFORMED_DATES:
            with self.subTest(text=text):
                self.new_scope()
                self.update_prompt("date")
                before = self.rows()
                result = self.turn(text)
                self.assertEqual(result["status"], "awaiting_update")
                self.assertTrue(result["invalid_input"])
                self.assertTrue(result["response"])
                self.assertNotIn("reservation_operation", result)
                self.assertEqual(self.rows(), before)
                self.assertEqual(self.state["editing_field"], "date")
                self.assertEqual(self.turn("8 September 2026")["status"], "updated")
                after = self.rows()[0]
                self.assertEqual(after[3], "2026-09-08")
                self.assertEqual(after[:3] + after[4:], before[0][:3] + before[0][4:])
                self.assertNotIn("pending_reservation_day", self.state)

    def test_blocker_combined_create_preserves_date_and_time(self):
        from tests.test_demo_blocker_fix import COMBINED_DATES
        for text in COMBINED_DATES:
            with self.subTest(text=text):
                self.new_scope(create=True)
                self.create_prompt(name="Dani Saputra", people=5, asked_fields=["date"])
                result = self.turn(text, create=True)
                self.assertEqual(result["status"], "awaiting_confirmation")
                self.assertEqual((self.state["date"], self.state["time"]), ("2026-09-06", "20:00"))
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.turn("ya", create=True)["status"], "completed")
                self.assertEqual(self.rows()[0][1:5], ("Dani Saputra", 5, "2026-09-06", "20:00"))

    def test_blocker_english_clocks_preserve_non_target_fields(self):
        for prefix in ("", "at ", "booking at ", "I am booking at ", "I am reserving for "):
            for period, expected in (("am", "11:00"), ("pm", "23:00")):
                with self.subTest(prefix=prefix, period=period):
                    self.new_scope(now=datetime(2026, 9, 5, 3, tzinfo=timezone.utc))
                    self.update_prompt("time")
                    before = self.rows()[0]
                    result = self.turn(prefix + "11 " + period, locale=SupportedLocale.EN_US)
                    self.assertEqual(result["status"], "updated")
                    self.assertIn("Time:", result["response"])
                    self.assertEqual(self.rows()[0][4], expected)
                    self.assertEqual(self.rows()[0][:4], before[:4])
                    self.assertIsNone(self.state.get("editing_field"))

    def test_blocker_combined_date_update_does_not_change_time(self):
        from tests.test_demo_blocker_fix import COMBINED_DATES
        for text in COMBINED_DATES:
            with self.subTest(text=text):
                self.new_scope()
                self.update_prompt("date")
                before = self.rows()[0]
                result = self.turn(text.replace("20:00", "21:00").replace("8 malam", "9 malam"))
                self.assertEqual(result["status"], "updated")
                after = self.rows()[0]
                self.assertEqual(after[3], "2026-09-06")
                self.assertEqual(after[:3] + after[4:], before[:3] + before[4:])
                self.assertNotIn("pending_reservation_day", self.state)

    def test_victor_create_requires_explicit_year_and_time_period(self):
        self.new_scope(create=True, now=datetime(2026, 9, 5, 15, 24, tzinfo=timezone.utc))
        self.create_prompt()
        opening = self.turn("Hai saya mau reservasi 9 orang atas nama Victor", create=True)
        self.assertEqual(opening["next_action"], "ask_date")
        result = self.turn("2 agustus", create=True)
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["next_action"], "ask_date")
        self.assertIn("tahun", result["response"])
        self.assertIsNone(self.state.get("date"))
        self.assertEqual((self.state["name"], self.state["people"]), ("Victor", 9))
        self.assertEqual(self.rows(), [])
        self.turn("ya", create=True)
        self.assertIsNone(self.state.get("date"))
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.turn("2 Agustus 2026", create=True)["invalid_input"])
        self.assertEqual(self.turn("2 Agustus 2027", create=True)["next_action"], "ask_time")
        ambiguous_time = self.turn("4", create=True)
        self.assertEqual(ambiguous_time["next_action"], "ask_time")
        self.assertIn("pagi", ambiguous_time["response"])
        self.assertIsNone(self.state.get("time"))
        confirmation = self.turn("jam 4 sore", create=True)
        self.assertEqual(confirmation["status"], "awaiting_confirmation")
        self.assertIn("2 Agustus 2027", confirmation["response"])
        self.assertEqual((self.state["date"], self.state["time"]), ("2027-08-02", "16:00"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("ya", create=True)["status"], "completed")
        self.assertEqual(self.rows()[0][1:5], ("Victor", 9, "2027-08-02", "16:00"))

    def test_create_year_clarification_preserves_combined_time_and_locale(self):
        for locale, text, year_word in (
            (SupportedLocale.ID_ID, "2 Agustus jam 4 sore", "tahun"),
            (SupportedLocale.EN_US, "August 2 at 4 pm", "year"),
        ):
            with self.subTest(locale=locale):
                self.new_scope(create=True)
                self.create_prompt(name="Victor", people=9, asked_fields=["date"])
                result = self.turn(text, create=True, locale=locale)
                self.assertEqual(result["next_action"], "ask_date")
                self.assertIn(year_word, result["response"])
                self.assertIsNone(self.state.get("date"))
                self.assertEqual(self.state["time"], "16:00")
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.turn("6 September 2026", create=True, locale=locale)["status"], "awaiting_confirmation")
                self.assertEqual((self.state["name"], self.state["people"], self.state["date"], self.state["time"]),
                                 ("Victor", 9, "2026-09-06", "16:00"))

    def test_confirmation_date_edits_cannot_silently_advance_year(self):
        for inline in (False, True):
            for locale in (SupportedLocale.ID_ID, SupportedLocale.EN_US):
                with self.subTest(inline=inline, locale=locale):
                    self.new_scope(create=True)
                    self.create_prompt(name="Victor", people=9, date="2026-09-06", time="16:00",
                                       awaiting_confirmation=True)
                    if not inline:
                        self.turn("ubah tanggal", create=True, locale=locale)
                    result = self.turn("ubah tanggal 2 Agustus" if inline else "2 Agustus",
                                       create=True, locale=locale)
                    self.assertTrue(result.get("invalid_input"))
                    self.assertEqual(self.state["date"], "2026-09-06")
                    self.assertEqual(self.state["editing_field"], "date")
                    self.assertEqual(self.rows(), [])
                    self.turn("ya", create=True, locale=locale)
                    self.assertEqual(self.rows(), [])
                    corrected = self.turn("2 Agustus 2027", create=True, locale=locale)
                    self.assertEqual(corrected["status"], "awaiting_confirmation")
                    self.assertIsNone(self.state.get("editing_field"))
                    self.assertEqual((self.state["date"], self.state["time"]), ("2027-08-02", "16:00"))
                    self.assertEqual(self.rows(), [])
                    self.assertEqual(self.turn("ya", create=True, locale=locale)["status"], "completed")
                    self.assertEqual(self.rows()[0][1:5], ("Victor", 9, "2027-08-02", "16:00"))

    def test_create_new_date_revalidates_collected_time_before_confirmation(self):
        self.new_scope(create=True, now=datetime(2026, 9, 5, 15, 24, tzinfo=timezone.utc))
        self.create_prompt(name="Victor", people=9, time="16:00", asked_fields=["date"])
        result = self.turn("5 September 2026", create=True)
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["next_action"], "ask_time")
        self.assertTrue(result["invalid_input"])
        self.assertEqual((self.state["name"], self.state["people"], self.state["date"]),
                         ("Victor", 9, "2026-09-05"))
        self.assertIsNone(self.state.get("time"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.turn("23:00", create=True)["status"], "awaiting_confirmation")
        self.assertEqual(self.turn("ya", create=True)["status"], "completed")
        self.assertEqual(self.rows()[0][1:5], ("Victor", 9, "2026-09-05", "23:00"))

    def test_confirmation_edit_revalidates_full_date_time_candidate(self):
        for field, value in (("date", "2 Agustus 2026"), ("date", "5 September 2026"), ("time", "07:00")):
            for inline in (False, True):
                with self.subTest(field=field, value=value, inline=inline):
                    self.new_scope(create=True, now=datetime(2026, 9, 5, 15, 24, tzinfo=timezone.utc))
                    initial_date, initial_time = ("2026-09-06", "16:00") if field == "date" else ("2026-09-05", "23:00")
                    self.create_prompt(name="Victor", people=9, date=initial_date, time=initial_time,
                                       awaiting_confirmation=True)
                    label = "tanggal" if field == "date" else "jam"
                    if not inline:
                        self.turn("ubah " + label, create=True)
                    result = self.turn("ubah " + label + " " + value if inline else value, create=True)
                    self.assertTrue(result.get("invalid_input"))
                    self.assertEqual((self.state["date"], self.state["time"]), (initial_date, initial_time))
                    self.assertEqual(self.state["editing_field"], field)
                    self.turn("ya", create=True)
                    self.assertEqual(self.rows(), [])
                    valid = "7 September 2026" if field == "date" else "23:30"
                    self.assertEqual(self.turn(valid, create=True)["status"], "awaiting_confirmation")
                    self.assertEqual(self.turn("ya", create=True)["status"], "completed")
                    self.assertEqual(self.rows()[0][1:5], ("Victor", 9,
                        "2026-09-07" if field == "date" else initial_date,
                        "23:30" if field == "time" else initial_time))

class LocalDialogueTests(DialogueContract, unittest.TestCase):
    setUp = fixture.PersistedReservationUpdateTests.setUp
