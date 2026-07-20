import asyncio
import unittest
from unittest.mock import MagicMock

from app.agents.orchestrator import AgentOrchestrator
from app.agents.cancel_reservation_agent import CancelReservationAgent
from app.agents.update_reservation_agent import UpdateReservationAgent


class TestReservationIntentRouting(unittest.TestCase):
    OWNER = "authenticated-owner"

    def test_flexible_update_phrase_routes_to_update_agent(self):
        orchestrator = AgentOrchestrator()
        handler = MagicMock()

        async def run(*_args):
            return {"status": "awaiting_update", "response": "Pilih ID"}

        handler.run = run
        orchestrator.update_reservation_agent = handler

        response = asyncio.run(
            orchestrator.handle(
                "internal-key",
                "reservasi saya ingin diubah",
                db=MagicMock(),
                owner_customer_id=self.OWNER,
            ),
        )

        self.assertEqual(response, "Pilih ID")

    def test_active_update_and_cancel_states_take_priority_over_new_text(self):
        orchestrator = AgentOrchestrator()
        calls = []

        class UpdateHandler:
            async def run(self, *_args):
                calls.append("update")
                return {"status": "awaiting_update", "response": "Update aktif"}

        class CancelHandler:
            async def run(self, *_args):
                calls.append("cancel")
                return {"status": "awaiting_cancellation", "response": "Cancel aktif"}

        orchestrator.update_reservation_agent = UpdateHandler()
        orchestrator.cancel_reservation_agent = CancelHandler()
        orchestrator.memory_manager.update_session(
            "update-key",
            {"update_reservation_stage": UpdateReservationAgent.SELECT_FIELD},
        )
        orchestrator.memory_manager.update_session(
            "cancel-key",
            {"cancel_reservation_stage": CancelReservationAgent.SELECT_RESERVATION_ID},
        )

        update_response = asyncio.run(
            orchestrator.handle(
                "update-key",
                "bagaimana cara mengubah reservasi?",
                db=MagicMock(),
                owner_customer_id=self.OWNER,
            ),
        )
        cancel_response = asyncio.run(
            orchestrator.handle(
                "cancel-key",
                "jangan batalkan reservasi saya",
                db=MagicMock(),
                owner_customer_id=self.OWNER,
            ),
        )

        self.assertEqual(update_response, "Update aktif")
        self.assertEqual(cancel_response, "Cancel aktif")
        self.assertEqual(calls, ["update", "cancel"])


if __name__ == "__main__":
    unittest.main()
