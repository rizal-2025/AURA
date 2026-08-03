import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from uuid import uuid4

from app.agents.orchestrator import AgentOrchestrator
from app.agents.result import (
    AgentTurnResult,
    ReservationOperationResult,
    ReservationOperationType,
)
from app.services.authenticated_chat_service import AuthenticatedChatService
from app.services.conversation_workflow_state_service import (
    WorkflowRestoreOutcome,
)


REFERENCE = "RSV_abcdefabcdefabcdefabcdefabcdefab"
SEEDED_TYPED_RESULT_RESERVATION_ID = (2**30) + 104_789


class _Handoff:
    @staticmethod
    def restore_active_handoff(*_args):
        return None


class _TypedAgent:
    handoff_service = _Handoff()

    def __init__(self):
        self.calls = 0

    async def handle_turn(self, **_kwargs):
        self.calls += 1
        return AgentTurnResult(
            reply=f"Referensi reservasi: {REFERENCE}",
            reservation_operation=ReservationOperationResult(
                operation=ReservationOperationType.CREATED,
                reference=REFERENCE,
            ),
        )


class _UnavailableWorkflow:
    def __init__(self):
        self.restore_calls = 0

    def restore(self, *_args, **_kwargs):
        self.restore_calls += 1
        return WorkflowRestoreOutcome.LEGACY_UNAVAILABLE


class AgentTurnResultTests(unittest.TestCase):
    def test_operation_is_immutable_canonical_and_repr_redacted(self):
        mixed = "rSv_ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        operation = ReservationOperationResult(
            ReservationOperationType.UPDATED,
            mixed,
        )

        self.assertEqual(operation.reference, REFERENCE)
        self.assertNotIn(REFERENCE, repr(operation))
        self.assertNotIn("id", vars(operation))
        self.assertNotIn("owner", vars(operation))
        with self.assertRaises(FrozenInstanceError):
            operation.reference = REFERENCE

    def test_typed_result_cannot_carry_exact_seeded_numeric_id(self):
        persisted_context = SimpleNamespace(
            id=SEEDED_TYPED_RESULT_RESERVATION_ID,
            reference=REFERENCE,
        )
        operation = ReservationOperationResult(
            ReservationOperationType.CREATED,
            persisted_context.reference,
        )
        result = AgentTurnResult(
            reply=f"Referensi reservasi: {persisted_context.reference}",
            reservation_operation=operation,
        )
        materialized = {
            "reply": result.reply,
            "operation": operation.operation.value,
            "reference": operation.reference,
        }
        boundary_text = "\n".join(
            (
                result.reply,
                repr(result),
                repr(operation),
                str(vars(result)),
                str(vars(operation)),
                str(materialized),
            )
        )

        self.assertNotIn(
            str(SEEDED_TYPED_RESULT_RESERVATION_ID),
            boundary_text,
        )
        self.assertEqual(set(vars(operation)), {"operation", "reference"})
        self.assertEqual(operation.reference, persisted_context.reference)
        self.assertNotIn(persisted_context.reference, repr(result))
        self.assertNotIn(persisted_context.reference, repr(operation))

    def test_invalid_operation_and_reference_fail_closed(self):
        with self.assertRaises(ValueError):
            ReservationOperationResult("created", REFERENCE)
        with self.assertRaises(ValueError):
            ReservationOperationResult(
                ReservationOperationType.CREATED,
                "RSV_invalid",
            )

    def test_turn_result_is_immutable_and_repr_omits_reply_and_reference(self):
        operation = ReservationOperationResult(
            ReservationOperationType.CANCELLED,
            REFERENCE,
        )
        result = AgentTurnResult(
            reply=f"Selesai {REFERENCE}",
            reservation_operation=operation,
        )

        self.assertNotIn(REFERENCE, repr(result))
        self.assertNotIn("Selesai", repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.reply = "changed"

    def test_orchestrator_uses_structured_metadata_not_reply_parsing(self):
        text_only = AgentOrchestrator._turn_result_from_agent_payload(
            {
                "response": (
                    f"Reservasi berhasil dibuat. Referensi reservasi: {REFERENCE}"
                )
            }
        )
        operation = ReservationOperationResult(
            ReservationOperationType.CREATED,
            REFERENCE,
        )
        structured = AgentOrchestrator._turn_result_from_agent_payload(
            {"response": "Selesai.", "reservation_operation": operation}
        )

        self.assertIsNone(text_only.reservation_operation)
        self.assertIs(structured.reservation_operation, operation)

    def test_authenticated_chat_exposes_typed_and_text_compatibility_paths(self):
        agent = _TypedAgent()
        service = AuthenticatedChatService(agent=agent)
        customer = SimpleNamespace(id=uuid4())

        typed = asyncio.run(
            service.process_turn(
                db=object(),
                customer=customer,
                session_reference="typed-session",
                message="buat reservasi",
            )
        )
        text = asyncio.run(
            service.process(
                db=object(),
                customer=customer,
                session_reference="text-session",
                message="buat reservasi",
            )
        )

        self.assertIs(type(typed), AgentTurnResult)
        self.assertEqual(
            typed.reservation_operation.operation,
            ReservationOperationType.CREATED,
        )
        self.assertEqual(text, f"Referensi reservasi: {REFERENCE}")
        self.assertEqual(agent.calls, 2)

    def test_legacy_unavailable_returns_fixed_reply_without_agent_call(self):
        agent = _TypedAgent()
        workflow = _UnavailableWorkflow()
        service = AuthenticatedChatService(
            agent=agent,
            workflow_state_service=workflow,
        )

        result = asyncio.run(
            service.process_turn(
                db=object(),
                customer=SimpleNamespace(id=uuid4()),
                session_reference="legacy-unavailable",
                message="lanjutkan",
            )
        )

        self.assertEqual(
            result.reply,
            "Sesi reservasi sebelumnya tidak dapat dipulihkan. "
            "Silakan mulai kembali dari daftar reservasi Anda.",
        )
        self.assertIsNone(result.reservation_operation)
        self.assertEqual(workflow.restore_calls, 1)
        self.assertEqual(agent.calls, 0)


if __name__ == "__main__":
    unittest.main()
