import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.reservation_agent import ReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.api.dependencies import get_current_customer
from app.api.error_handlers import _safe_validation_errors
from app.core.input_validation import (
    CHAT_MESSAGE_EMPTY,
    CHAT_MESSAGE_TOO_LONG,
    CHAT_MESSAGE_UNSAFE,
    CHAT_SESSION_ID_INVALID,
    InputValidationError,
    normalize_chat_message,
    normalize_reservation_name,
    validate_reservation_date,
    validate_reservation_people,
    validate_reservation_time,
    validate_session_reference,
)
from app.db.database import get_db
from app.main import app
from app.schemas.chat import ChatRequest
from app.schemas.reservation import ReservationCreate
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.reservation.service import ReservationService
from app.utils.datetime_parser import DatetimeParser


class SharedChatValidationTests(unittest.TestCase):
    def test_session_reference_exact_boundaries_and_punctuation(self):
        for value in ("a", "A0._-z", "a" * 128):
            with self.subTest(length=len(value)):
                self.assertEqual(validate_session_reference(value), value)
        for value in (
            "",
            "a" * 129,
            ".abc",
            "_abc",
            "-abc",
            "with space",
            "é-session",
            "slash/value",
            "colon:value",
            "line\nvalue",
        ):
            with self.subTest(kind=repr(value[:8])):
                with self.assertRaises(InputValidationError) as raised:
                    validate_session_reference(value)
                self.assertEqual(raised.exception.code, CHAT_SESSION_ID_INVALID)

    def test_message_boundaries_normalization_and_unicode(self):
        self.assertEqual(normalize_chat_message("a"), "a")
        self.assertEqual(normalize_chat_message("x" * 4096), "x" * 4096)
        self.assertEqual(
            normalize_chat_message("Halo\r\nApa kabar?\rBaik 🙂!"),
            "Halo\nApa kabar?\nBaik 🙂!",
        )
        self.assertEqual(
            normalize_chat_message("Pesan meja untuk besok.\nTerima kasih 🇮🇩"),
            "Pesan meja untuk besok.\nTerima kasih 🇮🇩",
        )

    def test_message_rejects_empty_overlong_and_unsafe_unicode(self):
        cases = (
            ("", CHAT_MESSAGE_EMPTY),
            (" \n ", CHAT_MESSAGE_EMPTY),
            ("x" * 4097, CHAT_MESSAGE_TOO_LONG),
            ("halo\0", CHAT_MESSAGE_UNSAFE),
            ("halo\u0085", CHAT_MESSAGE_UNSAFE),
            ("halo\u200b", CHAT_MESSAGE_UNSAFE),
            ("halo\u202e", CHAT_MESSAGE_UNSAFE),
            ("halo\t", CHAT_MESSAGE_UNSAFE),
        )
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(InputValidationError) as raised:
                    normalize_chat_message(value)
                self.assertEqual(raised.exception.code, code)

    def test_chat_schema_is_strict_and_forbids_extra_fields(self):
        normalized = ChatRequest(
            session_id="chat-01",
            message="Halo\r\nAURA",
        )
        self.assertEqual(normalized.message, "Halo\nAURA")
        for payload in (
            {"session_id": 1, "message": "Halo"},
            {"session_id": "chat", "message": 1},
            {"session_id": "chat", "message": "Halo", "owner_customer_id": "x"},
        ):
            with self.subTest(payload_keys=tuple(payload)):
                with self.assertRaises(ValidationError):
                    ChatRequest(**payload)

    def test_safe_exception_does_not_retain_rejected_value(self):
        rejected = "raw-session/value-that-must-not-leak"
        try:
            validate_session_reference(rejected)
        except InputValidationError as error:
            output = str(error) + repr(error) + repr(error.args)
        else:
            self.fail("Expected validation error")
        self.assertNotIn(rejected, output)


class ReservationValidationTests(unittest.TestCase):
    def test_name_normalizes_nfc_spaces_and_allowed_unicode(self):
        self.assertEqual(
            normalize_reservation_name("  Jose\u0301   D\u2019Angelo & A.J.  "),
            "Jos\u00e9 D\u2019Angelo & A.J.",
        )
        for value in ("A", "A" * 100, "Rizal-2", "Siti Nur.A & B"):
            with self.subTest(value=value[:8]):
                self.assertEqual(normalize_reservation_name(value), value)

    def test_name_rejects_bounds_unsafe_and_disallowed_characters(self):
        for value in (
            "",
            " " * 10,
            "A" * 101,
            "Nama\tOrang",
            "Nama\u00a0Orang",
            "Nama\u200bOrang",
            "Nama\u202eOrang",
            "Nama/Orang",
            "Nama_Orang",
            "Nama🙂",
            "\u0301\u0301",
            "\ufe0f\ufe0f",
            "...",
            "'''",
            "---",
            "&&&",
        ):
            with self.subTest(kind=repr(value[:8])):
                with self.assertRaises(InputValidationError):
                    normalize_reservation_name(value)

    def test_people_is_strict_and_bounded(self):
        self.assertEqual(validate_reservation_people(1), 1)
        self.assertEqual(validate_reservation_people(20), 20)
        for value in (True, False, 0, 21, -1, 1.0, Decimal("1"), "1"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(InputValidationError):
                    validate_reservation_people(value)

    def test_date_is_real_canonical_and_allows_past(self):
        for value in ("2028-02-29", "2000-01-01"):
            self.assertEqual(validate_reservation_date(value), value)
        for value in (
            "2027-02-29",
            "2026-13-01",
            "2026-7-01",
            "01-07-2026",
            "2026-01-01T00:00",
            "２０２６-０１-０１",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    validate_reservation_date(value)

    def test_time_is_canonical_and_bounded(self):
        for value in ("00:00", "23:59"):
            self.assertEqual(validate_reservation_time(value), value)
        for value in ("24:00", "12:60", "7:00", "07:00:00", "07:00+07:00"):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    validate_reservation_time(value)

    def test_reservation_schema_forbids_ownership_and_lifecycle_fields(self):
        valid = {
            "name": "  Jos\u00e9   D\u2019Angelo ",
            "people": 4,
            "date": "2000-01-01",
            "time": "19:00",
        }
        configured = ReservationCreate(**valid)
        self.assertEqual(configured.name, "Jos\u00e9 D\u2019Angelo")
        for field in ("owner_customer_id", "customer_id", "status", "id"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    ReservationCreate(**valid, **{field: "attacker-value"})

    def test_create_and_update_use_same_canonical_values(self):
        reservation_agent = ReservationAgent()
        update_agent = UpdateReservationAgent()
        cases = (
            ("name", "  Jose\u0301   D\u2019Angelo  ", "Jos\u00e9 D\u2019Angelo"),
            ("people", "menjadi 20 orang", 20),
            ("date", "2028-02-29", "2028-02-29"),
            ("time", "23:59", "23:59"),
        )
        for field, raw, expected in cases:
            with self.subTest(field=field):
                create_value = reservation_agent._infer_value_for_field(field, raw)
                update_value = update_agent._parse_new_value(field, raw)
                self.assertEqual(create_value, expected)
                self.assertEqual(update_value, expected)

    def test_create_and_update_reject_the_same_invalid_values(self):
        reservation_agent = ReservationAgent()
        update_agent = UpdateReservationAgent()
        cases = (
            ("name", "Nama\tPelanggan"),
            ("people", "menjadi 21 orang"),
            ("date", "2027-02-29"),
            ("time", "24:00"),
        )
        for field, raw in cases:
            with self.subTest(field=field):
                self.assertIsNone(
                    reservation_agent._infer_value_for_field(field, raw)
                )
                self.assertIsNone(update_agent._parse_new_value(field, raw))

    def test_invalid_service_update_never_reaches_repository(self):
        service = ReservationService()
        service.repository = MagicMock()
        with self.assertRaises(InputValidationError):
            service.update_reservation_field(
                MagicMock(),
                1,
                "people",
                21,
                owner_customer_id=uuid4(),
            )
        service.repository.update_reservation_field.assert_not_called()

    def test_create_service_revalidates_fresh_fields_and_preserves_owner(self):
        service = ReservationService()
        service.repository = MagicMock()
        owner_customer_id = uuid4()
        def persist(_db, validated, **_kwargs):
            return SimpleNamespace(
                id=7,
                **validated.model_dump(),
                status="pending",
            )

        service.repository.create.side_effect = persist
        data = ReservationCreate(
            name="  José   D’Angelo ",
            people=4,
            date="2028-02-29",
            time="19:00",
        )

        result = service.create_reservation(
            MagicMock(),
            data,
            owner_customer_id=owner_customer_id,
        )

        self.assertEqual(result.id, 7)
        forwarded = service.repository.create.call_args.args[1]
        self.assertIsNot(forwarded, data)
        self.assertEqual(forwarded.name, "José D’Angelo")
        self.assertEqual(forwarded.people, 4)
        self.assertEqual(
            service.repository.create.call_args.kwargs["owner_customer_id"],
            owner_customer_id,
        )

    def test_mutated_create_models_never_reach_repository(self):
        mutations = (
            ("name", "unsafe/name"),
            ("people", 999),
            ("date", "2027-02-29"),
            ("time", "24:00"),
        )
        for field_name, value in mutations:
            with self.subTest(field=field_name):
                service = ReservationService()
                service.repository = MagicMock()
                data = ReservationCreate(
                    name="Valid Name",
                    people=4,
                    date="2028-02-29",
                    time="19:00",
                )
                setattr(data, field_name, value)

                with self.assertRaises(ValidationError):
                    service.create_reservation(
                        MagicMock(),
                        data,
                        owner_customer_id=uuid4(),
                    )
                service.repository.create.assert_not_called()

    def test_model_construct_cannot_bypass_create_validation(self):
        service = ReservationService()
        service.repository = MagicMock()
        untrusted = ReservationCreate.model_construct(
            name="unsafe/name",
            people=999,
            date="not-a-date",
            time="99:99",
            owner_customer_id=uuid4(),
            status="confirmed",
            id=999,
        )

        with self.assertRaises(ValidationError):
            service.create_reservation(
                MagicMock(),
                untrusted,
                owner_customer_id=uuid4(),
            )
        service.repository.create.assert_not_called()

    def test_constructed_extra_identity_cannot_replace_authenticated_owner(self):
        service = ReservationService()
        service.repository = MagicMock()
        trusted_owner = uuid4()
        attacker_owner = uuid4()
        constructed = ReservationCreate.model_construct(
            name="Valid Name",
            people=4,
            date="2028-02-29",
            time="19:00",
            owner_customer_id=attacker_owner,
            status="cancelled",
            id=999,
        )
        service.repository.create.return_value = SimpleNamespace(
            id=8,
            name="Valid Name",
            people=4,
            date="2028-02-29",
            time="19:00",
            status="pending",
        )

        service.create_reservation(
            MagicMock(),
            constructed,
            owner_customer_id=trusted_owner,
        )

        forwarded = service.repository.create.call_args.args[1]
        self.assertEqual(
            set(forwarded.model_dump()),
            {"name", "people", "date", "time"},
        )
        self.assertEqual(
            service.repository.create.call_args.kwargs["owner_customer_id"],
            trusted_owner,
        )

    def test_datetime_parser_uses_injected_reference_date(self):
        reference = date(2026, 7, 18)
        self.assertEqual(
            DatetimeParser.parse_date("besok", reference_date=reference),
            "2026-07-19",
        )
        self.assertEqual(
            DatetimeParser.parse_date("hari Jumat", reference_date=reference),
            "2026-07-24",
        )


class SharedServiceBoundaryTests(unittest.TestCase):
    def test_http_schema_and_shared_service_normalize_message_identically(self):
        class Handoff:
            def restore_active_handoff(self, *_args, **_kwargs):
                return None

        class CapturingAgent:
            handoff_service = Handoff()

            def __init__(self):
                self.call = None

            async def handle(self, **kwargs):
                self.call = kwargs
                return "safe-reply"

        request = ChatRequest(
            session_id="chat-01",
            message="Halo\r\nAURA\rBaik",
        )
        agent = CapturingAgent()
        result = asyncio.run(
            AuthenticatedChatService(agent=agent).process(
                db=MagicMock(),
                customer=SimpleNamespace(id=uuid4()),
                session_reference="chat-01",
                message="Halo\r\nAURA\rBaik",
            )
        )

        self.assertEqual(result, "safe-reply")
        self.assertEqual(request.message, "Halo\nAURA\nBaik")
        self.assertEqual(agent.call["message"], request.message)

    def test_invalid_input_fails_before_memory_handoff_database_or_ai(self):
        class ForbiddenHandoff:
            def restore_active_handoff(self, *_args, **_kwargs):
                raise AssertionError("handoff must not be accessed")

        class ForbiddenAgent:
            handoff_service = ForbiddenHandoff()

            async def handle(self, **_kwargs):
                raise AssertionError("agent must not be called")

        service = AuthenticatedChatService(agent=ForbiddenAgent())
        db = MagicMock()
        customer = SimpleNamespace(id=uuid4())
        for session_reference, message in (
            ("bad/session", "Halo"),
            ("chat-safe", "unsafe\u200bmessage"),
        ):
            with self.subTest(session=session_reference):
                with self.assertRaises(InputValidationError):
                    asyncio.run(
                        service.process(
                            db=db,
                            customer=customer,
                            session_reference=session_reference,
                            message=message,
                        )
                    )
        db.assert_not_called()


class SafeHttpValidationTests(unittest.TestCase):
    def setUp(self):
        self.customer = SimpleNamespace(id=uuid4(), is_active=True, token_version=1)

        def override_db():
            yield MagicMock()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_customer] = lambda: self.customer
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_malformed_json_and_extra_fields_are_sanitized(self):
        raw_fragment = "raw-json-fragment-secret"
        malformed = self.client.post(
            "/chat",
            content='{"session_id":"chat","message":"' + raw_fragment,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.json()["code"], "REQUEST_VALIDATION_FAILED")
        self.assertEqual(
            malformed.json()["errors"],
            [{"field": "body", "code": "REQUEST_JSON_INVALID"}],
        )
        self.assertNotIn(raw_fragment, malformed.text)

        raw_message = "private-message-that-must-not-return"
        attacker_field = "owner_customer_id_private"
        extra = self.client.post(
            "/chat",
            json={
                "session_id": "chat-safe",
                "message": raw_message,
                attacker_field: "private-owner-value",
            },
        )
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(
            extra.json()["errors"],
            [{"field": "body", "code": "EXTRA_FIELD_FORBIDDEN"}],
        )
        self.assertNotIn(raw_message, extra.text)
        self.assertNotIn(attacker_field, extra.text)

    def test_reservation_extra_security_fields_are_sanitized(self):
        raw_name = "Private Customer Name"
        response = self.client.post(
            "/reservation/",
            json={
                "name": raw_name,
                "people": 4,
                "date": "2028-02-29",
                "time": "19:00",
                "owner_customer_id": "private-owner-id",
                "status": "confirmed",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "REQUEST_VALIDATION_FAILED")
        self.assertNotIn(raw_name, response.text)
        self.assertNotIn("private-owner-id", response.text)
        self.assertEqual(
            response.json()["errors"],
            [{"field": "body", "code": "EXTRA_FIELD_FORBIDDEN"}],
        )

    def test_application_rejects_oversized_body_before_endpoint(self):
        marker = "private-body-marker"
        body = (
            '{"session_id":"chat-safe","message":"'
            + marker
            + ("x" * 16_384)
            + '"}'
        )
        response = self.client.post(
            "/chat",
            content=body,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json(),
            {
                "code": "REQUEST_BODY_TOO_LARGE",
                "detail": "Request body is too large.",
            },
        )
        self.assertNotIn(marker, response.text)

    def test_authenticated_invalid_input_is_absent_from_logs_and_response(self):
        raw_session = "private-session-marker"
        raw_message = "private-message-marker\u200b"
        with self.assertLogs("AURA", level="INFO") as captured:
            response = self.client.post(
                "/chat",
                json={"session_id": raw_session, "message": raw_message},
            )

        output = "\n".join(captured.output)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(raw_session, response.text)
        self.assertNotIn(raw_message, response.text)
        self.assertNotIn(raw_session, output)
        self.assertNotIn(raw_message, output)

    def test_safe_validation_errors_are_deduplicated_and_capped(self):
        class ErrorCollection:
            def __init__(self, errors):
                self._errors = errors

            def errors(self):
                return self._errors

        duplicate_errors = ErrorCollection(
            [
                {"type": "extra_forbidden", "loc": ("body", "first")},
                {"type": "extra_forbidden", "loc": ("body", "second")},
            ]
        )
        self.assertEqual(
            _safe_validation_errors(duplicate_errors),
            [{"field": "body", "code": "EXTRA_FIELD_FORBIDDEN"}],
        )

        fields = ("session_id", "message", "name", "people", "date", "time")
        codes = (
            "CHAT_SESSION_ID_INVALID",
            "CHAT_MESSAGE_EMPTY",
            "CHAT_MESSAGE_TOO_LONG",
            "CHAT_MESSAGE_UNSAFE",
            "RESERVATION_NAME_INVALID",
            "RESERVATION_PEOPLE_INVALID",
            "RESERVATION_DATE_INVALID",
            "RESERVATION_TIME_INVALID",
            "CHAT_MESSAGE_INVALID",
        )
        many_errors = ErrorCollection(
            [
                {"type": code, "loc": ("body", fields[index % len(fields)])}
                for index, code in enumerate(codes)
            ]
        )
        self.assertEqual(len(_safe_validation_errors(many_errors)), 8)


if __name__ == "__main__":
    unittest.main()
