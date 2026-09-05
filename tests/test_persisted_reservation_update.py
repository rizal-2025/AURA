"""Update transaction boundaries with real durable workflow persistence."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_workflow_snapshot import mutation_blocker_snapshot_v2
from app.core.memory_errors import ConversationWorkflowPublicationError
from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.conversation_workflow_state import ConversationWorkflowState
from app.db.models.customer import Customer
from app.db.models.reservation import Reservation
from app.schemas.reservation import ReservationCreate
from app.services.conversation_workflow_state_service import ConversationWorkflowStateService
from app.services.reservation.service import ReservationService


NOW = datetime(2026, 9, 5, 5, 53, tzinfo=timezone.utc)


class PersistedUpdateContract:
    """Shared SQLite/PostgreSQL contract; neither workflow nor UoW is stubbed."""

    def prepare_update(self, field):
        self.db = self.Session()
        self.addCleanup(self.db.close)
        owner = Customer()
        self.db.add(owner)
        self.db.commit()
        self.owner_id = owner.id
        self.service = ReservationService(clock=lambda: NOW)
        created = self.service.create_reservation(
            self.db,
            ReservationCreate(name="Sherly", people=2, date="2026-09-05", time="12:57"),
            owner_customer_id=self.owner_id,
        )
        self.reference = created.reference
        self.key = "persisted-update-contract"
        self.memory = MemoryManager()
        self.workflow = ConversationWorkflowStateService(self.memory)
        self.memory.replace_reservation_workflow_state(self.key, {
            "update_reservation_stage": "input_value",
            "reservation_reference": self.reference,
            "editing_field": field,
        })
        self.memory.set_workflow_persistence_revision(self.key, 0)
        self.workflow.publish(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        # Exercise the same restore/activation boundary used by authenticated chat.
        self.workflow.restore(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        self.agent = UpdateReservationAgent(
            memory_manager=self.memory,
            reservation_service=self.service,
            workflow_state_service=self.workflow,
            clock=lambda: NOW,
        )
        self.initial_workflow = self.durable_workflow()
        self.nested_attempts = 0
        original_enter = UnitOfWork.__enter__

        def enter(unit):
            if getattr(unit.session, "_aura_transaction_owner", None) is not None:
                self.nested_attempts += 1
            return original_enter(unit)

        self.spy(UnitOfWork, "__enter__", new=enter)
        self.update_spy = self.spy(
            self.service.repository, "update_reservation_field_by_public_reference",
            wraps=self.service.repository.update_reservation_field_by_public_reference,
        )
        self.validation_spy = self.spy(
            self.service, "validate_new_reservation_datetime",
            wraps=self.service.validate_new_reservation_datetime,
        )
        self.marker_spy = self.spy(
            self.workflow, "begin_mutation", wraps=self.workflow.begin_mutation,
        )
        self.dml = []

        def track_dml(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                self.dml.append(statement)

        event.listen(self.engine, "before_cursor_execute", track_dml)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", track_dml)

    def spy(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def send_update(self, value):
        return asyncio.run(self.agent.run(self.db, self.key, value, self.owner_id))

    def durable_workflow(self):
        with self.Session() as db:
            row = db.scalar(select(ConversationWorkflowState).where(
                ConversationWorkflowState.owner_customer_id == self.owner_id,
            ))
            return row.revision, row.is_active, deepcopy(row.payload)

    def reservation_fields(self):
        with self.Session() as db:
            row = db.scalar(select(Reservation).where(Reservation.public_reference == self.reference))
            return row.name, row.people, row.date, row.time

    def assert_valid_update(self, field, value, expected):
        self.prepare_update(field)
        result = self.send_update(value)
        self.assertEqual(result["status"], "updated")
        self.assertIn("reservation_operation", result)
        self.assertEqual(self.nested_attempts, 0)
        self.assertEqual(self.update_spy.call_count, 1)
        self.assertEqual(self.marker_spy.call_count, 1)
        self.assertEqual(self.validation_spy.call_count, int(field in {"date", "time"}))
        self.assertEqual(self.reservation_fields(), expected)
        self.assertEqual(self.durable_workflow()[2], mutation_blocker_snapshot_v2("update").materialize())
        # The authenticated request publishes completion only after domain success.
        self.workflow.publish(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        self.assertEqual(self.durable_workflow()[1:], (False, {}))
        self.assertEqual(self.nested_attempts, 0)

    def test_persisted_valid_date_update(self):
        self.assert_valid_update("date", "6 September 2026", ("Sherly", 2, "2026-09-06", "12:57"))

    def test_persisted_valid_time_update(self):
        self.assert_valid_update("time", "13:30", ("Sherly", 2, "2026-09-05", "13:30"))

    def test_persisted_valid_name_update(self):
        self.assert_valid_update("name", "Sheryl", ("Sheryl", 2, "2026-09-05", "12:57"))

    def test_persisted_valid_people_update(self):
        self.assert_valid_update("people", "4", ("Sherly", 4, "2026-09-05", "12:57"))

    def assert_invalid_update(self, field, value):
        self.prepare_update(field)
        with patch("app.agents.update_reservation_agent.publish_update_success") as success:
            result = self.send_update(value)
        self.assertEqual(result["status"], "awaiting_update")
        self.assertTrue(result["invalid_input"])
        self.assertNotIn("reservation_operation", result)
        success.assert_not_called()
        self.update_spy.assert_not_called()
        self.marker_spy.assert_not_called()
        self.validation_spy.assert_called_once()
        self.assertEqual(self.dml, [])
        self.assertEqual(self.nested_attempts, 0)
        self.assertEqual(self.reservation_fields(), ("Sherly", 2, "2026-09-05", "12:57"))
        self.assertEqual(self.durable_workflow(), self.initial_workflow)

    def test_persisted_past_date_rejected(self):
        self.assert_invalid_update("date", "12 juli 2025")

    def test_persisted_past_time_rejected(self):
        self.assert_invalid_update("time", "12:30")

    def test_marker_failure_prevents_reservation_update(self):
        self.prepare_update("date")
        with patch.object(self.workflow.repository, "replace", side_effect=RuntimeError("marker failure")):
            with self.assertRaises(ConversationWorkflowPublicationError):
                self.send_update("6 September 2026")
        self.update_spy.assert_not_called()
        self.assertEqual(self.nested_attempts, 0)
        self.assertEqual(self.reservation_fields(), ("Sherly", 2, "2026-09-05", "12:57"))
        self.assertEqual(self.durable_workflow(), self.initial_workflow)

    def test_update_failure_rolls_back_reservation_and_keeps_durable_marker(self):
        self.prepare_update("date")
        original_update = self.service.repository.update_reservation_field_by_public_reference

        def fail_after_write(*args, **kwargs):
            original_update(*args, **kwargs)
            raise RuntimeError("reservation failure after SQL update")

        with patch.object(self.service.repository, "update_reservation_field_by_public_reference", side_effect=fail_after_write):
            with self.assertRaises(PersistenceOperationError):
                self.send_update("6 September 2026")
        self.assertEqual(self.nested_attempts, 0)
        self.assertEqual(self.reservation_fields(), ("Sherly", 2, "2026-09-05", "12:57"))
        self.assertEqual(self.durable_workflow()[2], mutation_blocker_snapshot_v2("update").materialize())
        # A restart before request-level publication must remain blocked.
        recovered = MemoryManager()
        ConversationWorkflowStateService(recovered).restore(
            self.db, owner_customer_id=self.owner_id, memory_key=self.key,
        )
        recovered_agent = UpdateReservationAgent(memory_manager=recovered, reservation_service=self.service)
        result = asyncio.run(recovered_agent.run(self.db, self.key, "6 September 2026", self.owner_id))
        self.assertEqual(result["status"], "persistence_uncertain")
        # The surviving request may publish its confirmed rollback/retry state.
        self.workflow.publish(self.db, owner_customer_id=self.owner_id, memory_key=self.key)
        self.assertEqual(self.durable_workflow()[2], self.initial_workflow[2])


class PersistedReservationUpdateTests(PersistedUpdateContract, unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def sqlite_constraint_functions(connection, _record):
            connection.create_function("char_length", 1, len)
            connection.create_function("jsonb_typeof", 1, lambda value: "object" if isinstance(json.loads(value), dict) else "other")

        for model in (Customer, Reservation, ConversationWorkflowState):
            model.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
