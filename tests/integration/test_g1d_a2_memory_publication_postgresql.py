"""Opt-in PostgreSQL verification for G1D-A2.1 memory publication.

This module requires an explicit TEST_DATABASE_URL, never falls back to
DATABASE_URL, creates model tables directly in a disposable schema, and runs no
application migration.
"""

import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_workflow_snapshot import (
    capture_reservation_workflow_snapshot_v2,
)
from app.brain.reservation_memory import (
    COMMITTED_MEMORY_UNAVAILABLE,
    COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
)
from app.core.config import settings
from app.core.memory_errors import (
    ConversationMemoryValidationError,
    PostCommitMemoryPublicationError,
)
from app.core.transaction_errors import PersistenceOperationError
from app.db.models.customer import Customer
from app.db.models.conversation_workflow_state import (
    ConversationWorkflowState,
)
from app.db.models.reservation import Reservation
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_notification import SupportTicketNotification
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate
from app.services.reservation.dto import PersistedReservationDTO
from app.services.reservation.service import ReservationService
from app.services.conversation_workflow_state_service import (
    ConversationWorkflowStateService,
)
from tests.integration.disposable_schema import DisposableSchemaResources


def _identity(url):
    parsed = make_url(url)
    return parsed.get_backend_name(), parsed.host, parsed.port, parsed.database


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return (
            "TEST_DATABASE_URL is not configured; G1D-A2.1 PostgreSQL tests "
            "are skipped."
        )
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if not parsed.database or "test" not in parsed.database.lower():
            return (
                "TEST_DATABASE_URL must name a disposable database containing "
                "'test'."
            )
        if _identity(value) == _identity(settings.DATABASE_URL):
            return (
                "TEST_DATABASE_URL resolves to the normal database; refusing "
                "to run."
            )
    except Exception:
        return (
            "TEST_DATABASE_URL is invalid; G1D-A2.1 PostgreSQL tests are "
            "skipped."
        )
    return None


SKIP_REASON = _skip_reason()
SEEDED_POSTGRESQL_RESERVATION_ID = (2**30) + 104_827


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestG1DA2MemoryPublicationPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(
            os.environ["TEST_DATABASE_URL"],
            pool_pre_ping=True,
        )
        cls.addClassCleanup(cls.admin.dispose)

    def setUp(self):
        self.schema = f"aura_g1d_a2_test_{uuid4().hex[:12]}"
        self.resources = DisposableSchemaResources(
            admin_engine=self.admin,
            schema=self.schema,
            allowed_prefixes=("aura_g1d_a2_test_",),
            dispose_admin=False,
        )
        self.addCleanup(self.resources.cleanup)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))

        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema},public"}
        )
        self.engine = create_engine(url, pool_pre_ping=True)
        self.resources.track_engine(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            expire_on_commit=True,
        )
        Customer.__table__.create(self.engine)
        Reservation.__table__.create(self.engine)
        ConversationWorkflowState.__table__.create(self.engine)
        SupportTicket.__table__.create(self.engine)
        SupportTicketNotification.__table__.create(self.engine)
        self.owner = uuid4()
        with self.Session.begin() as db:
            db.add(Customer(id=self.owner))

    @staticmethod
    def _data(people=4):
        return ReservationCreate(
            name="Rizal",
            people=people,
            date="2026-08-01",
            time="19:00",
        )

    @staticmethod
    def _seed_create(memory, key):
        memory.get_session(key).update(
            {
                "intent": "reservation",
                "name": "Rizal",
                "people": 4,
                "date": "2026-08-01",
                "time": "19:00",
                "completed": False,
                "awaiting_confirmation": True,
                "editing_field": None,
                "asked_fields": ["name"],
            }
        )

    def _count(self, model):
        with self.Session() as db:
            return db.scalar(select(func.count()).select_from(model))

    def _seed_next_reservation_id(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT setval("
                    "pg_get_serial_sequence('reservations', 'id'), "
                    ":seeded_id, false)"
                ),
                {"seeded_id": SEEDED_POSTGRESQL_RESERVATION_ID},
            )

    def test_successful_create_publishes_public_reference_without_numeric_id(self):
        self._seed_next_reservation_id()
        memory = MemoryManager()
        key = "owner:create-success"
        self._seed_create(memory, key)
        agent = ReservationAgent(memory_manager=memory)

        with self.Session() as db:
            result = asyncio.run(
                agent.handle_confirmation(
                    "Ya",
                    key,
                    owner_customer_id=self.owner,
                    db=db,
                )
            )

        reference = memory.get_session(key)["reservation_reference"]
        self.assertIn(reference, result["response"])
        with self.Session() as db:
            row = db.scalar(
                select(Reservation).where(
                    Reservation.public_reference == reference
                )
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.id, SEEDED_POSTGRESQL_RESERVATION_ID)
            self.assertNotIn(
                str(SEEDED_POSTGRESQL_RESERVATION_ID),
                result["response"],
            )
            self.assertNotIn(
                str(SEEDED_POSTGRESQL_RESERVATION_ID),
                str(memory.get_session(key)),
            )
            self.assertNotIn(f"ID Reservasi: {row.id}", result["response"])
            self.assertNotIn(f"ID: {row.id}", result["response"])

    def test_seeded_id_is_absent_from_real_v2_workflow_persistence(self):
        self._seed_next_reservation_id()
        with self.Session() as db:
            created = ReservationService().create_reservation(
                db,
                self._data(),
                owner_customer_id=self.owner,
            )
        self.assertEqual(created.id, SEEDED_POSTGRESQL_RESERVATION_ID)

        memory = MemoryManager()
        key = "owner:seeded-v2-workflow"
        memory.update_session(
            key,
            {
                "update_reservation_stage": UpdateReservationAgent.INPUT_VALUE,
                "reservation_reference": created.reference,
                "editing_field": "people",
            },
        )
        snapshot = capture_reservation_workflow_snapshot_v2(
            memory,
            key,
        ).materialize()
        workflow = ConversationWorkflowStateService(memory)
        with self.Session() as db:
            workflow.publish(
                db,
                owner_customer_id=self.owner,
                memory_key=key,
            )
        with self.Session() as db:
            row = db.scalar(
                select(ConversationWorkflowState).where(
                    ConversationWorkflowState.owner_customer_id == self.owner,
                    ConversationWorkflowState.session_reference_hash
                    == workflow.hash_session_reference(key),
                )
            )
            persisted_payload = dict(row.payload)
            persisted_schema_version = row.schema_version
            persisted_revision = row.revision
            persisted_is_active = row.is_active

        agent = UpdateReservationAgent(memory, ReservationService())
        with self.Session() as db:
            result = asyncio.run(agent.run(db, key, "7", self.owner))
        serialized = json.dumps(
            persisted_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        boundary_text = "\n".join(
            (
                result["response"],
                repr(result["reservation_operation"]),
                str(memory.get_session(key)),
                json.dumps(snapshot, sort_keys=True),
                serialized,
            )
        )

        self.assertNotIn(
            str(SEEDED_POSTGRESQL_RESERVATION_ID),
            boundary_text,
        )
        self.assertEqual(
            persisted_payload["reservation_reference"],
            created.reference,
        )
        self.assertEqual(persisted_schema_version, 2)
        self.assertEqual(persisted_revision, 1)
        self.assertTrue(persisted_is_active)
        self.assertNotIn("reservation_id", persisted_payload)
        self.assertNotIn("reservation_id", memory.get_session(key))

    def test_forced_precommit_create_failure_restores_memory_and_no_row(self):
        class FailingRepository(ReservationRepository):
            def create(self, *args, **kwargs):
                super().create(*args, **kwargs)
                raise RuntimeError("controlled pre-commit failure")

        memory = MemoryManager()
        key = "owner:create-failure"
        self._seed_create(memory, key)
        before = memory.snapshot_conversation(key).materialize()
        agent = ReservationAgent(memory_manager=memory)
        agent.reservation_service = ReservationService(FailingRepository())

        with self.Session() as db:
            with self.assertRaises(PersistenceOperationError):
                asyncio.run(
                    agent.handle_confirmation(
                        "Ya",
                        key,
                        owner_customer_id=self.owner,
                        db=db,
                    )
                )

        self.assertEqual(memory.get_session(key), before)
        self.assertEqual(self._count(Reservation), 0)

    def test_create_commit_then_publication_failure_blocks_duplicate_row(self):
        memory = MemoryManager()
        key = "owner:create-publication"
        self._seed_create(memory, key)
        agent = ReservationAgent(memory_manager=memory)

        with self.Session() as db, patch.object(
            memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(
                    agent.handle_confirmation(
                        "Ya",
                        key,
                        owner_customer_id=self.owner,
                        db=db,
                    )
                )

        self.assertEqual(self._count(Reservation), 1)
        self.assertEqual(
            memory.get_reservation_mutation_guard(key),
            {
                "status": COMMITTED_MEMORY_UNAVAILABLE,
                "operation": "create",
            },
        )
        with self.Session() as db:
            asyncio.run(
                agent.handle_confirmation(
                    "Ya",
                    key,
                    owner_customer_id=self.owner,
                    db=db,
                )
            )
        self.assertEqual(self._count(Reservation), 1)

    def test_update_commit_then_publication_failure_blocks_second_update(self):
        with self.Session() as db:
            created = ReservationService().create_reservation(
                db,
                self._data(),
                owner_customer_id=self.owner,
            )
        memory = MemoryManager()
        key = "owner:update-publication"
        memory.get_session(key).update(
            {
                "reservation_reference": created.reference,
                "editing_field": "people",
                "update_reservation_stage": UpdateReservationAgent.INPUT_VALUE,
            }
        )
        service = ReservationService()
        agent = UpdateReservationAgent(memory, service)

        with self.Session() as db, patch.object(
            memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(agent.run(db, key, "7", self.owner))

        with self.Session() as db:
            self.assertEqual(db.get(Reservation, created.id).people, 7)
            asyncio.run(agent.run(db, key, "8", self.owner))
        with self.Session() as db:
            self.assertEqual(db.get(Reservation, created.id).people, 7)

    def test_cancel_commit_then_publication_failure_blocks_second_cancel(self):
        with self.Session() as db:
            created = ReservationService().create_reservation(
                db,
                self._data(),
                owner_customer_id=self.owner,
            )
        memory = MemoryManager()
        key = "owner:cancel-publication"
        memory.get_session(key).update(
            {
                "cancel_reservation_reference": created.reference,
                "cancel_reservation_stage": (
                    CancelReservationAgent.CONFIRM_CANCELLATION
                ),
            }
        )
        service = ReservationService()
        service.cancel_reservation_by_reference = MagicMock(
            wraps=service.cancel_reservation_by_reference
        )
        agent = CancelReservationAgent(memory, service)

        with self.Session() as db, patch.object(
            memory,
            "replace_conversation",
            side_effect=ConversationMemoryValidationError(),
        ):
            with self.assertRaises(PostCommitMemoryPublicationError):
                asyncio.run(agent.run(db, key, "Ya", self.owner))

        with self.Session() as db:
            self.assertEqual(
                db.get(Reservation, created.id).status,
                "cancelled",
            )
            asyncio.run(agent.run(db, key, "Ya", self.owner))
        service.cancel_reservation_by_reference.assert_called_once()

    def test_formatter_failure_keeps_commit_and_creates_no_handoff_rows(self):
        memory = MemoryManager()
        key = "owner:create-format"
        self._seed_create(memory, key)
        orchestrator = AgentOrchestrator()
        orchestrator.memory_manager = memory
        orchestrator.workflow = orchestrator.workflow.__class__(
            memory_manager=memory
        )
        reservation_agent = orchestrator.workflow._agents["reservation"]

        with patch.object(
            reservation_agent,
            "_create_success_response",
            side_effect=RuntimeError("controlled formatting failure"),
        ), self.Session() as db:
            response = asyncio.run(
                orchestrator.handle(
                    key,
                    "Ya",
                    db,
                    owner_customer_id=self.owner,
                )
            )

        self.assertEqual(
            response,
            COMMITTED_OPERATION_FORMAT_FALLBACK_RESPONSE,
        )
        self.assertEqual(self._count(Reservation), 1)
        self.assertEqual(self._count(SupportTicket), 0)
        self.assertEqual(self._count(SupportTicketNotification), 0)

    def test_expire_on_commit_dto_is_detached_and_publication_safe(self):
        with self.Session() as db:
            result = ReservationService().create_reservation(
                db,
                self._data(),
                owner_customer_id=self.owner,
            )

        self.assertIsInstance(result, PersistedReservationDTO)
        self.assertGreater(result.id, 0)
        self.assertEqual(result.name, "Rizal")
        self.assertEqual(result.people, 4)


if __name__ == "__main__":
    unittest.main()
