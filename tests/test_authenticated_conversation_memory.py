import asyncio
import unittest

from app.agents.orchestrator import AgentOrchestrator
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.ownership import MissingOwnerCustomerError


class TestAuthenticatedConversationMemory(unittest.TestCase):
    def test_authenticated_key_scopes_same_client_session_to_each_owner(self):
        session_id = "chat-01"

        key_a = build_authenticated_memory_key("customer-a", session_id)
        key_b = build_authenticated_memory_key("customer-b", session_id)

        self.assertEqual(key_a, "customer-a:chat-01")
        self.assertEqual(key_b, "customer-b:chat-01")
        self.assertNotEqual(key_a, key_b)

    def test_missing_owner_is_rejected_before_memory_is_accessed(self):
        orchestrator = AgentOrchestrator()

        response = asyncio.run(
            orchestrator.handle("unscoped-session", "Halo", db=None),
        )

        self.assertEqual(
            response,
            "Identitas pelanggan tidak valid atau telah kedaluwarsa.",
        )
        self.assertNotIn("unscoped-session", orchestrator.memory_manager._sessions)
        with self.assertRaises(MissingOwnerCustomerError):
            build_authenticated_memory_key(None, "unscoped-session")


if __name__ == "__main__":
    unittest.main()
