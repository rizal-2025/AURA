"""Unit coverage for typed demo rate-limit policy and window semantics."""

from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models.demo_persistence import DemoRateLimitBucket
from app.services.demo_chat_errors import DemoChatServiceUnavailableError
from app.services.demo_rate_limit_service import (
    DEMO_RATE_LIMIT_POLICIES,
    DemoRateLimitAction,
    DemoRateLimitExceededError,
    DemoRateLimitService,
)
from app.services.demo_session_service import DemoSessionRequiredError


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _FailingBuckets:
    def consume_atomic(self, *_args, **_values):
        raise RuntimeError("database detail must not escape")


class _MissingSessionService:
    def resolve_active_session(self, *_args, **_values):
        return None


class DemoRateLimitServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        def configure(connection, _record):
            connection.create_function("char_length", 1, len)
            connection.create_function("jsonb_typeof", 1, lambda _v: "object")

        event.listen(self.engine, "connect", configure)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.now = datetime(2026, 8, 2, 10, 0, 1, tzinfo=timezone.utc)
        self.clock = _Clock(self.now)
        self.service = DemoRateLimitService(clock=self.clock)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def rows(self):
        return list(
            self.db.execute(
                select(DemoRateLimitBucket).order_by(
                    DemoRateLimitBucket.id
                )
            ).scalars()
        )

    def test_policy_table_is_typed_and_exact(self):
        flattened = {
            (policy.action.value, policy.scope_type): (
                policy.limit,
                policy.window_seconds,
            )
            for policies in DEMO_RATE_LIMIT_POLICIES.values()
            for policy in policies
        }
        self.assertEqual(
            flattened,
            {
                ("session_create", "global"): (30, 60),
                ("session_current", "session"): (60, 60),
                ("chat", "session"): (20, 60),
                ("chat", "global"): (300, 60),
                ("reservations_read", "session"): (30, 60),
                ("reset", "session"): (5, 3600),
            },
        )

    def test_first_request_is_allowed_and_persisted(self):
        decisions = self.service.enforce(
            self.db,
            action=DemoRateLimitAction.RESET,
            session_token_digest="a" * 64,
        )
        self.assertTrue(decisions[0].allowed)
        self.assertEqual(decisions[0].current_count, 1)
        self.assertEqual(decisions[0].remaining, 4)
        self.assertEqual(self.rows()[0].request_count, 1)

    def test_exact_limit_allowed_and_limit_plus_one_rejected(self):
        for _ in range(5):
            decisions = self.service.enforce(
                self.db,
                action=DemoRateLimitAction.RESET,
                session_token_digest="b" * 64,
            )
        self.assertTrue(decisions[0].allowed)
        with self.assertRaises(DemoRateLimitExceededError) as captured:
            self.service.enforce(
                self.db,
                action=DemoRateLimitAction.RESET,
                session_token_digest="b" * 64,
            )
        self.assertGreaterEqual(captured.exception.retry_after_seconds, 1)
        self.assertEqual(captured.exception.remaining, 0)
        self.assertEqual(self.rows()[0].request_count, 6)

    def test_window_rollover_creates_new_identity_at_exact_boundary(self):
        self.service.enforce(
            self.db,
            action=DemoRateLimitAction.SESSION_CURRENT,
            session_token_digest="c" * 64,
        )
        self.clock.value = datetime(
            2026,
            8,
            2,
            10,
            1,
            0,
            tzinfo=timezone.utc,
        )
        self.service.enforce(
            self.db,
            action=DemoRateLimitAction.SESSION_CURRENT,
            session_token_digest="c" * 64,
        )
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.request_count for row in rows], [1, 1])

    def test_repository_rejects_noncanonical_expiry_before_persistence(self):
        window_start, expires_at = self.service._window(
            self.now,
            60,
        )
        subject_digest = "7" * 64
        self.db.add(
            DemoRateLimitBucket(
                scope_type="session",
                subject_digest=subject_digest,
                action="chat",
                window_started_at=window_start,
                window_seconds=60,
                request_count=4,
                expires_at=expires_at,
                updated_at=self.now,
            )
        )
        self.db.commit()
        invalid_values = (
            {
                "window_started_at": window_start,
                "window_seconds": 60,
                "expires_at": expires_at - timedelta(seconds=1),
            },
            {
                "window_started_at": window_start,
                "window_seconds": 60,
                "expires_at": expires_at + timedelta(seconds=1),
            },
            {
                "window_started_at": window_start,
                "window_seconds": 60,
                "expires_at": expires_at + timedelta(microseconds=1),
            },
            {
                "window_started_at": window_start.replace(tzinfo=None),
                "window_seconds": 60,
                "expires_at": expires_at,
            },
            {
                "window_started_at": window_start + timedelta(seconds=1),
                "window_seconds": 60,
                "expires_at": expires_at + timedelta(seconds=1),
            },
            {
                "window_started_at": window_start,
                "window_seconds": 0,
                "expires_at": expires_at,
            },
        )
        statements = []

        def capture_sql(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture_sql)
        try:
            for values in invalid_values:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError) as captured:
                        self.service.buckets.consume_atomic(
                            self.db,
                            scope_type="session",
                            subject_digest=subject_digest,
                            action="chat",
                            now=self.now,
                            **values,
                        )
                    rendered = repr(captured.exception)
                    self.assertNotIn(subject_digest, rendered)
                    self.assertNotIn(str(window_start), rendered)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_sql)
        self.assertEqual(statements, [])
        row = self.rows()[0]
        self.assertEqual(row.request_count, 4)
        retained_expiry = row.expires_at
        if retained_expiry.tzinfo is None:
            retained_expiry = retained_expiry.replace(tzinfo=timezone.utc)
        self.assertEqual(retained_expiry, expires_at)

    def test_conflict_never_extends_an_existing_expiry(self):
        window_start, canonical_expiry = self.service._window(
            self.now,
            60,
        )
        shorter_expiry = canonical_expiry - timedelta(seconds=20)
        self.db.add(
            DemoRateLimitBucket(
                scope_type="session",
                subject_digest="9" * 64,
                action="chat",
                window_started_at=window_start,
                window_seconds=60,
                request_count=1,
                expires_at=shorter_expiry,
                updated_at=self.now,
            )
        )
        self.db.commit()

        count = self.service.buckets.consume_atomic(
            self.db,
            scope_type="session",
            subject_digest="9" * 64,
            action="chat",
            window_started_at=window_start,
            window_seconds=60,
            expires_at=canonical_expiry,
            now=self.now,
        )
        self.db.commit()

        retained_expiry = self.rows()[0].expires_at
        if retained_expiry.tzinfo is None:
            retained_expiry = retained_expiry.replace(tzinfo=timezone.utc)
        self.assertEqual(count, 2)
        self.assertEqual(retained_expiry, shorter_expiry)

    def test_exact_canonical_expiry_is_immutable_on_conflict(self):
        window_start, canonical_expiry = self.service._window(
            self.now,
            60,
        )
        for expected_count in (1, 2):
            with self.subTest(expected_count=expected_count):
                count = self.service.buckets.consume_atomic(
                    self.db,
                    scope_type="session",
                    subject_digest="0" * 64,
                    action="chat",
                    window_started_at=window_start,
                    window_seconds=60,
                    expires_at=canonical_expiry,
                    now=self.now,
                )
                self.db.commit()
                self.assertEqual(count, expected_count)
                retained_expiry = self.rows()[0].expires_at
                if retained_expiry.tzinfo is None:
                    retained_expiry = retained_expiry.replace(
                        tzinfo=timezone.utc
                    )
                self.assertEqual(retained_expiry, canonical_expiry)

    def test_conflict_never_shortens_an_existing_expiry(self):
        window_start, canonical_expiry = self.service._window(
            self.now,
            60,
        )
        longer_expiry = canonical_expiry + timedelta(seconds=60)
        self.db.add(
            DemoRateLimitBucket(
                scope_type="session",
                subject_digest="8" * 64,
                action="chat",
                window_started_at=window_start,
                window_seconds=60,
                request_count=1,
                expires_at=longer_expiry,
                updated_at=self.now,
            )
        )
        self.db.commit()

        count = self.service.buckets.consume_atomic(
            self.db,
            scope_type="session",
            subject_digest="8" * 64,
            action="chat",
            window_started_at=window_start,
            window_seconds=60,
            expires_at=canonical_expiry,
            now=self.now,
        )
        self.db.commit()

        retained_expiry = self.rows()[0].expires_at
        if retained_expiry.tzinfo is None:
            retained_expiry = retained_expiry.replace(tzinfo=timezone.utc)
        self.assertEqual(count, 2)
        self.assertEqual(retained_expiry, longer_expiry)

    def test_backward_clock_does_not_mutate_newer_bucket(self):
        self.clock.value = self.now + timedelta(hours=2)
        self.service.enforce(
            self.db,
            action=DemoRateLimitAction.RESET,
            session_token_digest="d" * 64,
        )
        self.clock.value = self.now
        self.service.enforce(
            self.db,
            action=DemoRateLimitAction.RESET,
            session_token_digest="d" * 64,
        )
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual({row.request_count for row in self.rows()}, {1})

    def test_sessions_do_not_share_bucket(self):
        for digest in ("e" * 64, "f" * 64):
            self.service.enforce(
                self.db,
                action=DemoRateLimitAction.RESET,
                session_token_digest=digest,
            )
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(
            {row.subject_digest for row in self.rows()},
            {"e" * 64, "f" * 64},
        )

    def test_actions_do_not_share_bucket(self):
        digest = "1" * 64
        for action in (
            DemoRateLimitAction.CHAT,
            DemoRateLimitAction.RESET,
        ):
            self.service.enforce(
                self.db,
                action=action,
                session_token_digest=digest,
            )
        actions = [row.action for row in self.rows()]
        self.assertEqual(actions.count("chat"), 2)
        self.assertEqual(actions.count("reset"), 1)

    def test_chat_consumes_session_and_global_in_same_attempt(self):
        decisions = self.service.enforce(
            self.db,
            action=DemoRateLimitAction.CHAT,
            session_token_digest="2" * 64,
        )
        self.assertEqual(len(decisions), 2)
        self.assertTrue(all(item.allowed for item in decisions))
        self.assertEqual(
            {(row.scope_type, row.request_count) for row in self.rows()},
            {("session", 1), ("global", 1)},
        )

    def test_global_chat_bucket_is_shared_across_sessions(self):
        for digest in ("3" * 64, "4" * 64):
            self.service.enforce(
                self.db,
                action=DemoRateLimitAction.CHAT,
                session_token_digest=digest,
            )
        global_row = next(
            row for row in self.rows() if row.scope_type == "global"
        )
        self.assertEqual(global_row.request_count, 2)

    def test_rejected_attempt_stays_committed(self):
        for _ in range(6):
            try:
                self.service.enforce(
                    self.db,
                    action=DemoRateLimitAction.RESET,
                    session_token_digest="5" * 64,
                )
            except DemoRateLimitExceededError:
                pass
        count = self.db.scalar(
            select(func.max(DemoRateLimitBucket.request_count))
        )
        self.assertEqual(count, 6)

    def test_raw_token_is_never_a_persisted_subject(self):
        raw_token = "R" * 43
        digest = __import__("hashlib").sha256(
            raw_token.encode("utf-8")
        ).hexdigest()
        self.service.enforce(
            self.db,
            action=DemoRateLimitAction.RESET,
            session_token_digest=digest,
        )
        rendered = " ".join(
            " ".join(
                (
                    row.subject_digest,
                    row.action,
                    row.scope_type,
                )
            )
            for row in self.rows()
        )
        self.assertNotIn(raw_token, rendered)
        self.assertIn(digest, rendered)

    def test_malformed_digest_fails_closed(self):
        with self.assertRaises(DemoChatServiceUnavailableError):
            self.service.enforce(
                self.db,
                action=DemoRateLimitAction.RESET,
                session_token_digest="not-a-digest",
            )

    def test_untyped_action_is_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            self.service.enforce(
                self.db,
                action="reset",
                session_token_digest="6" * 64,
            )
        self.assertEqual(self.rows(), [])

    def test_repository_failure_is_safe_503_signal(self):
        service = DemoRateLimitService(
            bucket_repository=_FailingBuckets(),
            clock=self.clock,
        )
        with self.assertRaises(DemoChatServiceUnavailableError) as captured:
            service.enforce(
                self.db,
                action=DemoRateLimitAction.SESSION_CREATE,
            )
        self.assertNotIn("database", repr(captured.exception).casefold())

    def test_valid_shaped_but_inactive_session_creates_no_bucket(self):
        service = DemoRateLimitService(
            session_service=_MissingSessionService(),
            clock=self.clock,
        )
        with self.assertRaises(DemoSessionRequiredError):
            service.resolve_active_session_digest(self.db, "Z" * 43)
        self.assertEqual(self.rows(), [])
