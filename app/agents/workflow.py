from typing import Any

from app.agents.reservation_agent import ReservationAgent
from app.agents.stub_agents import (
    CancelReservationAgent,
    CheckReservationAgent,
    GeneralQuestionAgent,
    GreetingAgent,
)
from app.brain.memory_manager import MemoryManager


class AgentWorkflow:
    """Execute a plan using the appropriate agent strategy."""

    def __init__(self, memory_manager: MemoryManager | None = None):
        shared_memory = memory_manager or MemoryManager()
        self._agents = {
            "reservation": ReservationAgent(memory_manager=shared_memory),
            "check_reservation": CheckReservationAgent(),
            "cancel_reservation": CancelReservationAgent(),
            "greeting": GreetingAgent(),
            "general_question": GeneralQuestionAgent(),
        }

    async def execute(self, plan: dict[str, Any], session_state: dict[str, Any], user_message: str, session_id: str | None = None) -> dict[str, Any]:
        intent = plan.get("intent", "general")
        steps = plan.get("steps", [])

        if not steps:
            return {
                "status": "no_steps",
                "response": "Tidak ada langkah yang dapat dijalankan.",
            }

        agent = self._agents.get(intent)
        if agent is None:
            return {
                "status": "unsupported_agent",
                "response": "Tidak ada agent yang tersedia untuk intent ini.",
            }

        return await agent.run(steps, session_state, user_message, session_id=session_id)
