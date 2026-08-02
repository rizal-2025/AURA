import asyncio
import io
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.orchestrator import AgentOrchestrator
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.api import chat as chat_api
from app.api.dependencies import get_current_customer
from app.api.error_handlers import conversation_busy_exception_handler
from app.brain.memory_manager import MemoryManager
from app.db.database import get_db
from app.main import app
from app.core.conversation_lock_manager import (
    ConversationBusyError,
    ConversationLockManager,
)
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.input_validation import InputValidationError
from app.core.ownership import MissingOwnerCustomerError
from app.integrations.telegram.handlers import TelegramCustomerHandlers
from app.integrations.telegram.identity import derive_telegram_session_reference
from app.integrations.telegram.owner_command_handlers import (
    TelegramOwnerCommandHandlers,
)
from app.services.authenticated_chat_service import (
    AuthenticatedChatService,
    conversation_lock_manager,
)
from app.services.handoff.owner_ticket_service import OwnerTicketResult


class FakeTicketService:
    def __init__(self):
        self.calls = []
        self.called = asyncio.Event()
        self.ticket = SimpleNamespace(ticket_number="CS-2026-000001")

    def get_active(self, db, *, owner_customer_id, memory_key):
        self.calls.append((db, owner_customer_id, memory_key))
        self.called.set()
        return self.ticket


class FakeHandoffService:
    def __init__(self):
        self.restore_calls = []
        self.ticket_service = FakeTicketService()

    def restore_active_handoff(self, memory_key, db, owner_customer_id):
        self.restore_calls.append((memory_key, db, owner_customer_id))

    @staticmethod
    def recovery_error_response():
        return "safe recovery response"


class ControlledAgent:
    def __init__(self, handler):
        self.handoff_service = FakeHandoffService()
        self.handler = handler
        self.calls = []

    async def handle(self, **kwargs):
        self.calls.append(kwargs)
        return await self.handler(**kwargs)


class WorkflowReservationService:
    """Controlled repository boundary used by the real Update/Cancel agents."""

    def __init__(self, owner_customer_id):
        self.owner_customer_id = owner_customer_id
        self.reservations = {
            7: SimpleNamespace(
                id=7,
                name="Ayu",
                people=4,
                date="2026-08-20",
                time="19:00",
                status="pending",
                owner_customer_id=owner_customer_id,
                reference="RSV_77777777777777777777777777777777",
            ),
        }

    def list_recent_reservations(self, _db, owner_customer_id, limit=5):
        if owner_customer_id != self.owner_customer_id:
            return []
        return list(self.reservations.values())[:limit]

    def get_reservation_by_reference(self, _db, reference, owner_customer_id):
        reservation = next(
            (
                item
                for item in self.reservations.values()
                if item.reference == reference
            ),
            None,
        )
        if (
            reservation is None
            or owner_customer_id != self.owner_customer_id
            or reservation.owner_customer_id != owner_customer_id
        ):
            return None
        return reservation

    def update_reservation_field_by_reference(
        self,
        _db,
        reference,
        field_name,
        new_value,
        owner_customer_id,
    ):
        reservation = self.get_reservation_by_reference(
            _db,
            reference,
            owner_customer_id,
        )
        if reservation is None:
            return None
        setattr(reservation, field_name, new_value)
        return reservation

    def cancel_reservation_by_reference(self, _db, reference, owner_customer_id):
        reservation = self.get_reservation_by_reference(
            _db,
            reference,
            owner_customer_id,
        )
        if reservation is None or reservation.status == "cancelled":
            return None
        reservation.status = "cancelled"
        return reservation


class WorkflowHandoffStub:
    """No-network handoff boundary; workflow state machines remain real."""

    def __init__(self):
        self.ticket_service = FakeTicketService()

    def restore_active_handoff(self, *_args):
        return None

    def is_required(self, _memory_key):
        return False

    def reset_misunderstandings(self, _memory_key):
        pass

    def reset_ambiguity(self, _memory_key):
        pass

    def reset_invalid_input(self, _memory_key):
        pass

    def record_invalid_input(self, _memory_key, _workflow, _stage):
        return 1


def real_workflow_orchestrator(memory_manager, reservation_service):
    """Build the real orchestrator state routing without constructing an AI client."""
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.memory_manager = memory_manager
    orchestrator.update_reservation_agent = UpdateReservationAgent(
        memory_manager=memory_manager,
        reservation_service=reservation_service,
    )
    orchestrator.cancel_reservation_agent = CancelReservationAgent(
        memory_manager=memory_manager,
        reservation_service=reservation_service,
    )
    orchestrator.handoff_service = WorkflowHandoffStub()
    return orchestrator


class BlockingWorkflowAgent:
    def __init__(self, orchestrator, blocked_message):
        self.orchestrator = orchestrator
        self.handoff_service = orchestrator.handoff_service
        self.blocked_message = blocked_message
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()
        self._blocked = False

    async def handle(self, **kwargs):
        if kwargs["message"] == self.blocked_message and not self._blocked:
            self._blocked = True
            self.first_entered.set()
            await self.release_first.wait()
        return await self.orchestrator.handle(**kwargs)


class ConversationSerializationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_falsy_injected_agent_and_manager_are_preserved(self):
        class FalsyAgent(ControlledAgent):
            def __bool__(self):
                return False

        class FalsyManager(ConversationLockManager):
            def __bool__(self):
                return False

        async def handler(**_kwargs):
            return "injected"

        agent = FalsyAgent(handler)
        manager = FalsyManager()
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=manager,
        )
        response = await service.process(
            db=object(),
            customer=SimpleNamespace(id=uuid4()),
            session_reference="falsy-injection",
            message="halo",
        )

        self.assertIs(service.agent, agent)
        self.assertIs(service.lock_manager, manager)
        self.assertEqual(response, "injected")
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_same_owner_and_session_serialize_in_arrival_order(self):
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        messages = []

        async def handler(**kwargs):
            messages.append(kwargs["message"])
            if kwargs["message"] == "first":
                first_entered.set()
                await release_first.wait()
            return kwargs["message"]

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )
        customer = SimpleNamespace(id=uuid4())
        first = asyncio.create_task(service.process(
            db=object(), customer=customer, session_reference="chat", message="first"
        ))
        await first_entered.wait()
        second = asyncio.create_task(service.process(
            db=object(), customer=customer, session_reference="chat", message="second"
        ))
        await asyncio.sleep(0)
        self.assertEqual(messages, ["first"])
        release_first.set()
        self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
        self.assertEqual(messages, ["first", "second"])

    async def test_different_sessions_and_different_owners_remain_concurrent(self):
        entered = []
        all_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(**kwargs):
            entered.append(kwargs["session_id"])
            if len(entered) == 3:
                all_entered.set()
            await release.wait()
            return "ok"

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )
        owner_a = SimpleNamespace(id=uuid4())
        owner_b = SimpleNamespace(id=uuid4())
        tasks = [
            asyncio.create_task(service.process(
                db=object(), customer=owner_a, session_reference="one", message="first"
            )),
            asyncio.create_task(service.process(
                db=object(), customer=owner_a, session_reference="two", message="second"
            )),
            asyncio.create_task(service.process(
                db=object(), customer=owner_b, session_reference="one", message="third"
            )),
        ]
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        self.assertEqual(len(set(entered)), 3)
        release.set()
        await asyncio.gather(*tasks)

    async def test_ticket_status_serializes_with_state_changing_process(self):
        process_entered = asyncio.Event()
        release_process = asyncio.Event()

        async def handler(**_kwargs):
            process_entered.set()
            await release_process.wait()
            return "processed"

        agent = ControlledAgent(handler)
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ConversationLockManager(),
        )
        customer = SimpleNamespace(id=uuid4())
        process_task = asyncio.create_task(service.process(
            db=object(), customer=customer, session_reference="chat", message="first"
        ))
        await process_entered.wait()
        status_task = asyncio.create_task(service.ticket_status(
            db=object(), customer=customer, session_reference="chat"
        ))
        await asyncio.sleep(0)
        self.assertEqual(agent.handoff_service.ticket_service.calls, [])
        release_process.set()
        await process_task
        response = await status_task
        self.assertIn("CS-2026-000001", response)

    async def test_update_and_cancel_state_transitions_cannot_interleave(self):
        state = {"stage": "update_select"}
        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        observations = []

        async def handler(**kwargs):
            if kwargs["message"] == "update":
                observations.append(("update-start", state["stage"]))
                update_entered.set()
                await release_update.wait()
                state["stage"] = "update_input"
                observations.append(("update-end", state["stage"]))
                return "update"
            observations.append(("cancel-start", state["stage"]))
            state["stage"] = "cancel_select"
            return "cancel"

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )
        customer = SimpleNamespace(id=uuid4())
        update_task = asyncio.create_task(service.process(
            db=object(), customer=customer,
            session_reference="workflow", message="update",
        ))
        await update_entered.wait()
        cancel_task = asyncio.create_task(service.process(
            db=object(), customer=customer,
            session_reference="workflow", message="cancel",
        ))
        await asyncio.sleep(0)
        self.assertEqual(observations, [("update-start", "update_select")])
        release_update.set()
        await asyncio.gather(update_task, cancel_task)
        self.assertEqual(observations, [
            ("update-start", "update_select"),
            ("update-end", "update_input"),
            ("cancel-start", "update_input"),
        ])

    async def test_real_update_turns_preserve_selected_id_field_and_stage(self):
        customer = SimpleNamespace(id=uuid4())
        memory = MemoryManager()
        reservation_service = WorkflowReservationService(customer.id)
        orchestrator = real_workflow_orchestrator(memory, reservation_service)
        agent = BlockingWorkflowAgent(
            orchestrator,
            blocked_message="RSV_77777777777777777777777777777777",
        )
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ConversationLockManager(),
        )
        memory_key = build_authenticated_memory_key(customer.id, "real-update")
        session = memory.get_session(memory_key)
        session["update_reservation_stage"] = (
            UpdateReservationAgent.SELECT_RESERVATION_REFERENCE
        )

        select_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="real-update",
            message="RSV_77777777777777777777777777777777",
        ))
        await agent.first_entered.wait()
        field_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="real-update",
            message="jumlah orang",
        ))
        await asyncio.sleep(0)

        self.assertFalse(field_task.done())
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_RESERVATION_REFERENCE,
        )
        agent.release_first.set()
        selected_response, field_response = await asyncio.wait_for(
            asyncio.gather(select_task, field_task),
            timeout=1,
        )

        self.assertIn("Reservasi dipilih", selected_response)
        self.assertIn("Jumlah orang baru", field_response)
        self.assertEqual(
            session["reservation_reference"],
            "RSV_77777777777777777777777777777777",
        )
        self.assertEqual(session["editing_field"], "people")
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.INPUT_VALUE,
        )

    async def test_real_cancel_turns_cannot_overtake_selection(self):
        customer = SimpleNamespace(id=uuid4())
        memory = MemoryManager()
        reservation_service = WorkflowReservationService(customer.id)
        orchestrator = real_workflow_orchestrator(memory, reservation_service)
        agent = BlockingWorkflowAgent(
            orchestrator,
            blocked_message="RSV_77777777777777777777777777777777",
        )
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ConversationLockManager(),
        )
        memory_key = build_authenticated_memory_key(customer.id, "real-cancel")
        session = memory.get_session(memory_key)
        session["cancel_reservation_stage"] = (
            CancelReservationAgent.SELECT_RESERVATION_REFERENCE
        )

        select_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="real-cancel",
            message="RSV_77777777777777777777777777777777",
        ))
        await agent.first_entered.wait()
        confirm_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="real-cancel",
            message="Ya",
        ))
        await asyncio.sleep(0)

        self.assertFalse(confirm_task.done())
        self.assertEqual(
            session["cancel_reservation_stage"],
            CancelReservationAgent.SELECT_RESERVATION_REFERENCE,
        )
        agent.release_first.set()
        selected_response, confirm_response = await asyncio.wait_for(
            asyncio.gather(select_task, confirm_task),
            timeout=1,
        )

        self.assertIn("Reservasi dipilih", selected_response)
        self.assertIn("berhasil dibatalkan", confirm_response)
        self.assertEqual(reservation_service.reservations[7].status, "cancelled")
        session = memory.get_session(memory_key)
        self.assertIsNone(session["cancel_reservation_stage"])
        self.assertIsNone(session["cancel_reservation_reference"])

    async def test_real_update_stage_prevents_cancel_from_overwriting_intermediate_state(self):
        customer = SimpleNamespace(id=uuid4())
        memory = MemoryManager()
        reservation_service = WorkflowReservationService(customer.id)
        orchestrator = real_workflow_orchestrator(memory, reservation_service)
        agent = BlockingWorkflowAgent(
            orchestrator,
            blocked_message="RSV_77777777777777777777777777777777",
        )
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ConversationLockManager(),
        )
        memory_key = build_authenticated_memory_key(customer.id, "mixed-workflow")
        session = memory.get_session(memory_key)
        session["update_reservation_stage"] = (
            UpdateReservationAgent.SELECT_RESERVATION_REFERENCE
        )

        select_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="mixed-workflow",
            message="RSV_77777777777777777777777777777777",
        ))
        await agent.first_entered.wait()
        cancel_task = asyncio.create_task(service.process(
            db=object(),
            customer=customer,
            session_reference="mixed-workflow",
            message="batalkan reservasi saya",
        ))
        await asyncio.sleep(0)
        self.assertFalse(cancel_task.done())

        agent.release_first.set()
        _selected, cancel_response = await asyncio.wait_for(
            asyncio.gather(select_task, cancel_task),
            timeout=1,
        )
        self.assertIn("Field tidak valid", cancel_response)
        self.assertEqual(
            session["reservation_reference"],
            "RSV_77777777777777777777777777777777",
        )
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.SELECT_FIELD,
        )
        self.assertIsNone(session.get("cancel_reservation_stage"))

        field_response = await service.process(
            db=object(),
            customer=customer,
            session_reference="mixed-workflow",
            message="jumlah orang",
        )
        self.assertIn("Jumlah orang baru", field_response)
        self.assertEqual(session["editing_field"], "people")
        self.assertEqual(
            session["update_reservation_stage"],
            UpdateReservationAgent.INPUT_VALUE,
        )

    async def test_real_workflow_different_sessions_enter_concurrently(self):
        customer = SimpleNamespace(id=uuid4())
        memory = MemoryManager()
        reservation_service = WorkflowReservationService(customer.id)
        orchestrator = real_workflow_orchestrator(memory, reservation_service)
        all_entered = asyncio.Event()
        release = asyncio.Event()
        entered = 0

        class ConcurrentRealWorkflowAgent:
            handoff_service = orchestrator.handoff_service

            async def handle(self, **kwargs):
                nonlocal entered
                entered += 1
                if entered == 2:
                    all_entered.set()
                await release.wait()
                return await orchestrator.handle(**kwargs)

        service = AuthenticatedChatService(
            agent=ConcurrentRealWorkflowAgent(),
            lock_manager=ConversationLockManager(),
        )
        for session_reference in ("real-one", "real-two"):
            memory_key = build_authenticated_memory_key(
                customer.id,
                session_reference,
            )
            memory.get_session(memory_key)["update_reservation_stage"] = (
                UpdateReservationAgent.SELECT_RESERVATION_REFERENCE
            )

        tasks = [
            asyncio.create_task(service.process(
                db=object(),
                customer=customer,
                session_reference=session_reference,
                message="RSV_77777777777777777777777777777777",
            ))
            for session_reference in ("real-one", "real-two")
        ]
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        self.assertEqual(entered, 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

        for session_reference in ("real-one", "real-two"):
            memory_key = build_authenticated_memory_key(
                customer.id,
                session_reference,
            )
            session = memory.get_session(memory_key)
            self.assertEqual(
                session["reservation_reference"],
                "RSV_77777777777777777777777777777777",
            )
            self.assertEqual(
                session["update_reservation_stage"],
                UpdateReservationAgent.SELECT_FIELD,
            )

    async def test_owner_telegram_command_does_not_acquire_customer_lock(self):
        owner_id = 246810

        class Db:
            def close(self):
                pass

            def rollback(self):
                pass

        class Tickets:
            def list_active_tickets(self, _db, limit=10):
                self.limit = limit
                return OwnerTicketResult("empty")

        class Message:
            def __init__(self):
                self.replies = []
                self.sender_chat = None
                self.forward_origin = None

            async def reply_text(self, text, **_kwargs):
                self.replies.append(text)

        message = Message()
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=owner_id, type="private"),
            effective_user=SimpleNamespace(id=owner_id),
        )
        handlers = TelegramOwnerCommandHandlers(
            owner_chat_id=owner_id,
            session_factory=Db,
            ticket_service=Tickets(),
        )

        with patch.object(
            conversation_lock_manager,
            "hold",
            side_effect=AssertionError(
                "owner commands must not acquire customer conversation locks"
            ),
        ) as hold:
            await handlers.tickets(update, SimpleNamespace(args=[]))

        hold.assert_not_called()
        self.assertEqual(message.replies, ["Tidak ada tiket bantuan aktif."])

    async def test_first_failure_does_not_block_next_request(self):
        call_count = 0

        async def handler(**_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("synthetic")
            return "recovered"

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )
        customer = SimpleNamespace(id=uuid4())
        with self.assertRaises(RuntimeError):
            await service.process(
                db=object(), customer=customer,
                session_reference="chat", message="first",
            )
        response = await service.process(
            db=object(), customer=customer,
            session_reference="chat", message="second",
        )
        self.assertEqual(response, "recovered")
        self.assertEqual(service.lock_manager.registry_size_for_test, 0)

    async def test_timeout_does_not_access_handoff_or_agent(self):
        manager = ConversationLockManager(wait_timeout_seconds=0.01)
        agent = ControlledAgent(AsyncMock(return_value="must not run"))
        service = AuthenticatedChatService(agent=agent, lock_manager=manager)
        customer = SimpleNamespace(id=uuid4())
        key = build_authenticated_memory_key(customer.id, "busy")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with manager.hold(key):
                entered.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        with self.assertRaises(ConversationBusyError):
            await service.process(
                db=object(), customer=customer,
                session_reference="busy", message="halo",
            )
        self.assertEqual(agent.handoff_service.restore_calls, [])
        self.assertEqual(agent.calls, [])
        release.set()
        await holder_task
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_invalid_owner_session_and_message_fail_before_lock(self):
        class ForbiddenLockManager:
            def hold(self, _key):
                raise AssertionError("invalid input must not acquire a lock")

        agent = ControlledAgent(AsyncMock(return_value="unused"))
        service = AuthenticatedChatService(
            agent=agent,
            lock_manager=ForbiddenLockManager(),
        )
        with self.assertRaises(MissingOwnerCustomerError):
            await service.process(
                db=object(), customer=SimpleNamespace(id=None),
                session_reference="chat", message="halo",
            )
        for session, message in (("bad/session", "halo"), ("chat", " \t ")):
            with self.subTest(session=session, message=message):
                with self.assertRaises(InputValidationError):
                    await service.process(
                        db=object(), customer=SimpleNamespace(id=uuid4()),
                        session_reference=session, message=message,
                    )
        self.assertEqual(agent.calls, [])

    async def test_serialized_handoff_creates_one_ticket_and_outbox(self):
        state = {"locked": False, "tickets": 0, "outbox": 0}
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def handler(**kwargs):
            if not state["locked"]:
                first_entered.set()
                await release_first.wait()
                state["locked"] = True
                state["tickets"] += 1
                state["outbox"] += 1
                return "created"
            return "existing"

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )
        customer = SimpleNamespace(id=uuid4())
        first = asyncio.create_task(service.process(
            db=object(), customer=customer,
            session_reference="handoff", message="petugas",
        ))
        await first_entered.wait()
        second = asyncio.create_task(service.process(
            db=object(), customer=customer,
            session_reference="handoff", message="petugas",
        ))
        await asyncio.sleep(0)
        release_first.set()
        self.assertEqual(await asyncio.gather(first, second), ["created", "existing"])
        self.assertEqual(state["tickets"], 1)
        self.assertEqual(state["outbox"], 1)

    async def test_http_and_telegram_ingress_share_one_logical_lock_in_process(self):
        identity_secret = "g1c-telegram-identity-secret-safe-material"
        user_id = 1001
        chat_id = 2001
        customer = SimpleNamespace(id=uuid4())
        session_reference = derive_telegram_session_reference(
            identity_secret,
            user_id,
            chat_id,
        )
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        identity_resolved = asyncio.Event()
        messages = []

        async def handler(**kwargs):
            messages.append(kwargs["message"])
            if kwargs["message"] == "http-first":
                first_entered.set()
                await release_first.wait()
            return kwargs["message"]

        service = AuthenticatedChatService(
            agent=ControlledAgent(handler),
            lock_manager=ConversationLockManager(),
        )

        class Db:
            def rollback(self):
                pass

            def close(self):
                pass

        class Identity:
            def resolve_or_create(self, *_args, **_kwargs):
                identity_resolved.set()
                return customer

        class Message:
            text = "telegram-second"

            def __init__(self):
                self.replies = []

            async def reply_text(self, text):
                self.replies.append(text)

        telegram_message = Message()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id, type="private"),
            effective_message=telegram_message,
        )
        telegram_handlers = TelegramCustomerHandlers(
            identity_secret=identity_secret,
            session_factory=Db,
            identity_service=Identity(),
            chat_service=service,
        )

        http_task = asyncio.create_task(service.process(
            db=Db(),
            customer=customer,
            session_reference=session_reference,
            message="http-first",
        ))
        await first_entered.wait()
        telegram_task = asyncio.create_task(
            telegram_handlers.text_message(update, None)
        )
        await identity_resolved.wait()
        await asyncio.sleep(0)
        self.assertEqual(messages, ["http-first"])
        release_first.set()
        await asyncio.gather(http_task, telegram_task)
        self.assertEqual(messages, ["http-first", "telegram-second"])
        self.assertEqual(telegram_message.replies, ["telegram-second"])

    async def test_http_busy_response_is_safe_and_logs_no_scope_values(self):
        owner = str(uuid4())
        session = "private-session"
        memory_key = f"{owner}:{session}"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        aura_logger = logging.getLogger("AURA")
        aura_logger.addHandler(handler)
        try:
            response = await conversation_busy_exception_handler(
                None,
                ConversationBusyError(),
            )
        finally:
            aura_logger.removeHandler(handler)

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.body)
        self.assertEqual(payload, {
            "code": "CONVERSATION_BUSY",
            "detail": "This conversation is still processing a previous message.",
        })
        combined = response.body.decode() + stream.getvalue()
        self.assertNotIn(owner, combined)
        self.assertNotIn(session, combined)
        self.assertNotIn(memory_key, combined)


class ConversationBusyHttpEndpointTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_authenticated_chat_timeout_returns_safe_409(self):
        owner = uuid4()
        session_reference = "http-busy-session"
        app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=owner)
        app.dependency_overrides[get_db] = lambda: object()
        with patch.object(
            chat_api.authenticated_chat_service,
            "process",
            AsyncMock(side_effect=ConversationBusyError()),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/chat",
                    json={"session_id": session_reference, "message": "halo"},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {
            "code": "CONVERSATION_BUSY",
            "detail": "This conversation is still processing a previous message.",
        })
        serialized = response.text
        self.assertNotIn(str(owner), serialized)
        self.assertNotIn(session_reference, serialized)


if __name__ == "__main__":
    unittest.main()
