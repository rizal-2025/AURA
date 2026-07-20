"""Endpoint-level conversation simulator used by customer-service scenarios."""

from dataclasses import dataclass
from typing import Any

from app.api.chat import agent as chat_agent
from app.brain.classifier import IntentClassifier
from app.core.conversation_memory import build_authenticated_memory_key
from app.core.security import create_customer_access_token


@dataclass
class ConversationResult:
    replies: list[str]
    intents: list[str]
    state: dict[str, Any]
    errors: list[str]
    handoff: bool = False
    ticket_category: str | None = None


class ConversationSimulator:
    """Run a JSON scenario through ``POST /chat`` without exposing tokens."""

    def __init__(self, client, customers: dict[str, Any]):
        self.client = client
        self.customers = customers

    def run(self, scenario: dict[str, Any]) -> ConversationResult:
        customer = self.customers[scenario["authenticated_customer"]]
        session_id = scenario["session_id"]
        memory_key = build_authenticated_memory_key(customer.id, session_id)
        chat_agent.memory_manager.clear_session(memory_key)

        token, _ = create_customer_access_token(customer.id, customer.token_version)
        headers = {"Authorization": f"Bearer {token}"}
        replies: list[str] = []
        intents: list[str] = []
        errors: list[str] = []

        for turn_number, message in enumerate(scenario["customer_messages"], start=1):
            response = self.client.post(
                "/chat",
                json={"session_id": session_id, "message": message},
                headers=headers,
            )
            if response.status_code != 200:
                errors.append(f"turn={turn_number} status={response.status_code}")
                continue

            replies.append(response.json().get("reply", ""))
            detected_intent = IntentClassifier.detect_reservation_intent(message)
            intents.append(detected_intent or "general")

        state = dict(chat_agent.memory_manager.get_session(memory_key))
        # The bearer token is deliberately local to this call and is never
        # returned, printed, or placed in the scenario result.
        return ConversationResult(
            replies=replies,
            intents=intents,
            state=state,
            errors=errors,
        )
