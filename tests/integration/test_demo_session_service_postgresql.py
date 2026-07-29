"""Disposable PostgreSQL tests for the internal demo-session service."""

from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.transaction_errors import PersistenceOperationError
from app.core.unit_of_work import UnitOfWork
from app.db.models.customer import Customer
from app.db.models.demo_persistence import (
    DemoChatMessage,
    DemoHandoffEvent,
    DemoRateLimitBucket,
    DemoSession,
    safe_demo_handoff_summary,
)
from app.db.repositories.demo_persistence_repository import (
    DemoChatMessageRepository,
    DemoHandoffEventRepository,
    DemoSessionRepository,
)
from app.services.demo_session_service import (
    DemoSessionRequiredError,
    DemoSessionService,
    digest_demo_session_token,
)
from migrations.add_demo_persistence import migrate
from migrations.add_demo_chat_request_id import migrate as migrate_request_id
from tests.integration.disposable_schema import DisposableSchemaResources


TOKEN_A = "P" * 43
TOKEN_B = "Q" * 43
TOKEN_C = "R" * 43


def _skip_reason():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        return (
            "TEST_DATABASE_URL is not configured; demo-session "
            "PostgreSQL tests are skipped."
        )
    try:
        parsed = make_url(value)
        if parsed.get_backend_name() != "postgresql":
            return "TEST_DATABASE_URL must target PostgreSQL."
        if parsed.database != "aura_test":
            return "TEST_DATABASE_URL must target the exact aura_test database."
    except Exception:
        return "TEST_DATABASE_URL is invalid; PostgreSQL tests are skipped."
    return None


SKIP_REASON = _skip_reason()


class _FailingSessionRepository(DemoSessionRepository):
    def create(self, db, **values):
        raise RuntimeError("forced safe rollback")


class _TouchAttemptRepository(DemoSessionRepository):
    def __init__(self, attempted: threading.Event):
        self.attempted = attempted

    def update_last_seen(self, db, **values):
        self.attempted.set()
        return super().update_last_seen(db, **values)


class _FailAfterTouchRepository(DemoSessionRepository):
    def update_last_seen(self, db, **values):
        super().update_last_seen(db, **values)
        raise RuntimeError("forced failure after touch")


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class DemoSessionServicePostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(
            os.environ["TEST_DATABASE_URL"],
            pool_pre_ping=True,
        )
        with cls.admin.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        if identity != ("aura_test", "aura_test_runner"):
            cls.admin.dispose()
            raise RuntimeError(
                "Dedicated PostgreSQL preflight identity did not match."
            )

        cls.schema = f"aura_demo_session_service_test_{uuid4().hex[:12]}"
        cls.resources = DisposableSchemaResources(
            admin_engine=cls.admin,
            schema=cls.schema,
            allowed_prefixes=("aura_demo_session_service_test_",),
            dispose_admin=True,
        )
        cls.addClassCleanup(cls.resources.cleanup)
        with cls.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))

        schema_url = make_url(
            os.environ["TEST_DATABASE_URL"]
        ).update_query_dict(
            {"options": f"-csearch_path={cls.schema},public"}
        )
        cls.engine = create_engine(schema_url, pool_pre_ping=True)
        cls.resources.track_engine(cls.engine)
        Customer.__table__.create(cls.engine)
        migrate(cls.engine, schema=cls.schema)
        migrate_request_id(cls.engine, schema=cls.schema)
        cls.Session = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def setUp(self):
        self.db = self.Session()
        for model in (
            DemoRateLimitBucket,
            DemoHandoffEvent,
            DemoChatMessage,
            DemoSession,
            Customer,
        ):
            self.db.execute(delete(model))
        self.db.commit()
        self.now = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)
        self.sessions = DemoSessionRepository()
        self.messages = DemoChatMessageRepository()
        self.handoffs = DemoHandoffEventRepository()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def service(self, token=TOKEN_A, now=None, repository=None):
        return DemoSessionService(
            token_generator=lambda: token,
            clock=lambda: now or self.now,
            session_repository=repository,
        )

    def create(self, token=TOKEN_A, now=None):
        response = self.service(token, now=now).create_session(self.db)
        row = self.db.scalar(
            select(DemoSession).where(
                DemoSession.token_digest
                == digest_demo_session_token(token)
            )
        )
        return response, row

    def test_customer_and_demo_session_commit_atomically(self):
        _response, row = self.create()
        customer = self.db.get(Customer, row.owner_customer_id)
        self.assertIsNotNone(customer)
        self.assertTrue(customer.is_active)
        self.assertEqual(customer.token_version, 1)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoSession)),
            1,
        )

    def test_failure_rolls_back_customer_without_orphan(self):
        service = self.service(
            repository=_FailingSessionRepository()
        )
        with self.assertRaises(PersistenceOperationError):
            service.create_session(self.db)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(Customer)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoSession)),
            0,
        )

    def test_digest_lookup_uses_real_database(self):
        _response, expected = self.create()
        self.db.expire_all()
        found = self.service().resolve_active_session(
            self.db,
            TOKEN_A,
        )
        self.assertEqual(found.id, expected.id)
        self.assertIsNone(
            self.service().resolve_active_session(self.db, TOKEN_B)
        )

    def test_two_session_ownership_is_isolated(self):
        _first_response, first = self.create(TOKEN_A)
        _second_response, second = self.create(TOKEN_B)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(
            first.owner_customer_id,
            second.owner_customer_id,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(Customer)),
            2,
        )

    def test_touch_expiry_is_committed_and_survives_reload(self):
        _response, row = self.create(
            now=self.now - timedelta(minutes=30)
        )
        absolute = row.absolute_expires_at
        self.service(now=self.now).get_current_session(
            self.db,
            TOKEN_A,
        )
        session_id = row.id
        self.db.close()
        self.db = self.Session()
        reloaded = self.db.get(DemoSession, session_id)
        self.assertEqual(reloaded.last_seen_at, self.now)
        self.assertEqual(
            reloaded.idle_expires_at,
            self.now + timedelta(hours=2),
        )
        self.assertEqual(reloaded.absolute_expires_at, absolute)

    def test_concurrent_older_touch_cannot_regress_newer_activity(self):
        created_at = self.now - timedelta(minutes=30)
        _response, first = self.create(TOKEN_A, now=created_at)
        _response, second = self.create(TOKEN_B, now=created_at)
        first_id = first.id
        second_id = second.id
        absolute_expiry = first.absolute_expires_at
        second_state = (
            second.last_seen_at,
            second.idle_expires_at,
            second.absolute_expires_at,
        )
        self.db.rollback()

        newer_now = self.now + timedelta(minutes=20)
        older_now = self.now + timedelta(minutes=10)
        newer_db = self.Session()
        attempted = threading.Event()
        finished = threading.Event()
        failures = []

        self.sessions.update_last_seen(
            newer_db,
            demo_session_id=first_id,
            idle_expires_at=newer_now + timedelta(hours=2),
            now=newer_now,
        )

        def complete_older_touch():
            older_db = self.Session()
            try:
                with UnitOfWork(older_db) as unit:
                    _TouchAttemptRepository(attempted).update_last_seen(
                        older_db,
                        demo_session_id=first_id,
                        idle_expires_at=older_now + timedelta(hours=2),
                        now=older_now,
                    )
                    unit.commit()
            except BaseException as error:
                failures.append(error)
            finally:
                older_db.close()
                finished.set()

        worker = threading.Thread(target=complete_older_touch)
        worker.start()
        self.assertTrue(attempted.wait(timeout=5))
        self.assertFalse(finished.wait(timeout=0.1))
        newer_db.commit()
        newer_db.close()
        self.assertTrue(finished.wait(timeout=5))
        worker.join(timeout=1)
        self.assertEqual(failures, [])

        self.db.expire_all()
        reloaded = self.db.get(DemoSession, first_id)
        other = self.db.get(DemoSession, second_id)
        self.assertEqual(reloaded.last_seen_at, newer_now)
        self.assertEqual(
            reloaded.idle_expires_at,
            newer_now + timedelta(hours=2),
        )
        self.assertEqual(reloaded.absolute_expires_at, absolute_expiry)
        self.assertLessEqual(
            reloaded.idle_expires_at,
            reloaded.absolute_expires_at,
        )
        self.assertEqual(
            (
                other.last_seen_at,
                other.idle_expires_at,
                other.absolute_expires_at,
            ),
            second_state,
        )

    def test_revoke_wins_against_waiting_current_touch(self):
        created_at = self.now - timedelta(minutes=30)
        _response, row = self.create(TOKEN_A, now=created_at)
        session_id = row.id
        original_seen = row.last_seen_at
        original_idle = row.idle_expires_at
        original_absolute = row.absolute_expires_at
        self.db.rollback()

        # Revoke holds the row lock; current reads the old snapshot, then waits
        # on the same lock before it can revalidate and attempt its touch.
        revoked_at = self.now + timedelta(minutes=5)
        revoker_db = self.Session()
        self.sessions.revoke(
            revoker_db,
            demo_session_id=session_id,
            now=revoked_at,
        )
        attempted = threading.Event()
        finished = threading.Event()
        outcomes = []

        def resolve_current():
            current_db = self.Session()
            try:
                service = self.service(
                    now=self.now + timedelta(minutes=10),
                    repository=_TouchAttemptRepository(attempted),
                )
                service.get_current_session(current_db, TOKEN_A)
                outcomes.append("unexpected-success")
            except DemoSessionRequiredError:
                outcomes.append("rejected")
            except BaseException as error:
                outcomes.append(error)
            finally:
                current_db.close()
                finished.set()

        worker = threading.Thread(target=resolve_current)
        worker.start()
        self.assertTrue(attempted.wait(timeout=5))
        self.assertFalse(finished.wait(timeout=0.1))
        revoker_db.commit()
        revoker_db.close()
        self.assertTrue(finished.wait(timeout=5))
        worker.join(timeout=1)
        self.assertEqual(outcomes, ["rejected"])

        self.db.expire_all()
        reloaded = self.db.get(DemoSession, session_id)
        self.assertEqual(reloaded.revoked_at, revoked_at)
        self.assertEqual(reloaded.last_seen_at, original_seen)
        self.assertEqual(reloaded.idle_expires_at, original_idle)
        self.assertEqual(reloaded.absolute_expires_at, original_absolute)

    def test_failure_after_touch_rolls_back_only_session_activity(self):
        created_at = self.now - timedelta(minutes=30)
        _response, first = self.create(TOKEN_A, now=created_at)
        _response, second = self.create(TOKEN_B, now=created_at)
        self.messages.append(
            self.db,
            demo_session_id=first.id,
            role="user",
            content="preserved-history",
            created_at=created_at,
        )
        self.db.commit()
        first_id = first.id
        second_id = second.id
        first_state = (
            first.last_seen_at,
            first.idle_expires_at,
            first.absolute_expires_at,
        )
        second_state = (
            second.last_seen_at,
            second.idle_expires_at,
            second.absolute_expires_at,
        )

        with self.assertRaises(PersistenceOperationError):
            self.service(
                now=self.now,
                repository=_FailAfterTouchRepository(),
            ).get_current_session(self.db, TOKEN_A)

        self.db.expire_all()
        reloaded_first = self.db.get(DemoSession, first_id)
        reloaded_second = self.db.get(DemoSession, second_id)
        self.assertEqual(
            (
                reloaded_first.last_seen_at,
                reloaded_first.idle_expires_at,
                reloaded_first.absolute_expires_at,
            ),
            first_state,
        )
        self.assertEqual(
            (
                reloaded_second.last_seen_at,
                reloaded_second.idle_expires_at,
                reloaded_second.absolute_expires_at,
            ),
            second_state,
        )
        self.assertEqual(
            self.messages.count_by_demo_session(
                self.db,
                demo_session_id=first_id,
            ),
            1,
        )

    def test_current_history_and_handoff_are_session_scoped(self):
        _response, first = self.create(TOKEN_A)
        _response, second = self.create(TOKEN_B)
        for number in range(55):
            self.messages.append(
                self.db,
                demo_session_id=first.id,
                role="user",
                content=f"first-{number:02d}",
                created_at=self.now + timedelta(seconds=number),
            )
        self.messages.append(
            self.db,
            demo_session_id=second.id,
            role="user",
            content="second-only",
            created_at=self.now,
        )
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=first.id,
            reference="DEMO-HO-POSTGRES-FIRST",
            reason_code="explicit_human_request",
            created_at=self.now,
        )
        self.handoffs.create_simulated(
            self.db,
            demo_session_id=second.id,
            reference="DEMO-HO-POSTGRES-SECOND",
            reason_code="internal_error",
            created_at=self.now,
        )
        self.db.commit()
        result = self.service(
            now=self.now + timedelta(minutes=1)
        ).get_current_session(self.db, TOKEN_A)
        self.assertEqual(len(result.messages), 50)
        self.assertEqual(result.session.message_count, 55)
        self.assertEqual(result.messages[0].content, "first-05")
        self.assertEqual(
            result.handoff.reference,
            "DEMO-HO-POSTGRES-FIRST",
        )

    def test_current_rebuilds_valid_handoff_summary_from_allowlist(self):
        _response, row = self.create(TOKEN_A)
        handoff = self.handoffs.create_simulated(
            self.db,
            demo_session_id=row.id,
            reference="DEMO-HO-UNTRUSTED-SUMMARY",
            reason_code="explicit_human_request",
            created_at=self.now,
        )
        self.db.commit()
        unsafe = (
            "<script>token-like-marker</script>\n"
            "SELECT password FROM secrets;"
        )
        self.db.execute(
            text(
                "UPDATE demo_handoff_events "
                "SET safe_summary=:unsafe WHERE id=:id"
            ),
            {"unsafe": unsafe, "id": handoff.id},
        )
        self.db.commit()
        self.db.expire_all()

        result = self.service(
            now=self.now + timedelta(minutes=1)
        ).get_current_session(self.db, TOKEN_A)
        self.assertEqual(
            result.handoff.safe_summary,
            safe_demo_handoff_summary("explicit_human_request"),
        )
        self.assertNotIn(unsafe, repr(result))
        for marker in ("<script>", "token-like-marker", "SELECT password"):
            self.assertNotIn(marker, result.handoff.safe_summary)

    def test_unknown_handoff_reason_uses_safe_fallback_and_stays_scoped(self):
        _response, first = self.create(TOKEN_A)
        _response, second = self.create(TOKEN_B)
        first_handoff = self.handoffs.create_simulated(
            self.db,
            demo_session_id=first.id,
            reference="DEMO-HO-UNKNOWN-REASON",
            reason_code="internal_error",
            created_at=self.now,
        )
        second_handoff = self.handoffs.create_simulated(
            self.db,
            demo_session_id=second.id,
            reference="DEMO-HO-OTHER-SESSION",
            reason_code="explicit_human_request",
            created_at=self.now,
        )
        self.db.commit()
        unsafe = "unknown-private-reason\nraw-summary-marker"
        self.db.execute(
            text(
                "UPDATE demo_handoff_events "
                "SET reason_code=:reason, safe_summary=:unsafe WHERE id=:id"
            ),
            {
                "reason": "unknown-private-reason",
                "unsafe": unsafe,
                "id": first_handoff.id,
            },
        )
        self.db.execute(
            text(
                "UPDATE demo_handoff_events "
                "SET safe_summary=:unsafe WHERE id=:id"
            ),
            {"unsafe": "other-session-secret", "id": second_handoff.id},
        )
        self.db.commit()
        self.db.expire_all()

        result = self.service(
            now=self.now + timedelta(minutes=1)
        ).get_current_session(self.db, TOKEN_A)
        self.assertEqual(result.handoff.reference, first_handoff.reference)
        self.assertEqual(result.handoff.reason_code, "internal_error")
        self.assertEqual(
            result.handoff.safe_summary,
            safe_demo_handoff_summary("internal_error"),
        )
        rendered = repr(result)
        for forbidden in (
            unsafe,
            "unknown-private-reason",
            "raw-summary-marker",
            second_handoff.reference,
            "other-session-secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_unique_owner_constraint_remains_enforced(self):
        _response, first = self.create(TOKEN_A)
        with self.assertRaises(PersistenceOperationError):
            with UnitOfWork(self.db) as unit:
                self.sessions.create(
                    self.db,
                    token_digest=digest_demo_session_token(TOKEN_B),
                    owner_customer_id=first.owner_customer_id,
                    now=self.now,
                    idle_expires_at=self.now + timedelta(hours=2),
                    absolute_expires_at=self.now + timedelta(hours=24),
                )
                unit.commit()
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoSession)),
            1,
        )

    def test_unique_digest_constraint_remains_enforced(self):
        self.create(TOKEN_A)
        owner = Customer()
        self.db.add(owner)
        self.db.commit()
        with self.assertRaises(PersistenceOperationError):
            with UnitOfWork(self.db) as unit:
                self.sessions.create(
                    self.db,
                    token_digest=digest_demo_session_token(TOKEN_A),
                    owner_customer_id=owner.id,
                    now=self.now,
                    idle_expires_at=self.now + timedelta(hours=2),
                    absolute_expires_at=self.now + timedelta(hours=24),
                )
                unit.commit()
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DemoSession)),
            1,
        )

    def test_expired_session_is_not_touched_or_revived(self):
        old_now = self.now - timedelta(hours=3)
        _response, row = self.create(TOKEN_A, now=old_now)
        original_seen = row.last_seen_at
        original_idle = row.idle_expires_at
        with self.assertRaises(DemoSessionRequiredError):
            self.service(now=self.now).get_current_session(
                self.db,
                TOKEN_A,
            )
        self.db.refresh(row)
        self.assertEqual(row.last_seen_at, original_seen)
        self.assertEqual(row.idle_expires_at, original_idle)

    def test_raw_token_is_never_stored(self):
        response, row = self.create()
        self.assertEqual(response.session_token, TOKEN_A)
        stored = self.db.execute(
            select(
                DemoSession.token_digest,
                DemoSession.owner_customer_id,
                DemoSession.environment_scope,
            ).where(DemoSession.id == row.id)
        ).one()
        self.assertEqual(
            stored.token_digest,
            digest_demo_session_token(TOKEN_A),
        )
        self.assertNotIn(TOKEN_A, repr(stored))
        self.assertNotIn(
            "token",
            {
                column.name
                for column in DemoSession.__table__.columns
                if column.name != "token_digest"
            },
        )


if __name__ == "__main__":
    unittest.main()
