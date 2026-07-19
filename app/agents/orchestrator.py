from app.agents.workflow import AgentWorkflow
from app.agents.update_reservation_agent import UpdateReservationAgent
from app.agents.view_reservation_agent import ViewReservationAgent
from app.brain.classifier import IntentClassifier
from app.brain.memory_manager import MemoryManager
from app.brain.planner import Planner
from app.core.logger import logger
from app.memory.session import memory
from app.services.ai.factory import get_ai_provider


class AgentOrchestrator:

    VIEW_RESERVATION_PHRASES = {
        "lihat reservasi saya",
        "reservasi saya",
        "daftar reservasi",
        "show my reservation",
    }
    UPDATE_RESERVATION_PHRASES = {
        "ubah reservasi saya",
        "edit reservasi saya",
        "update reservasi saya",
        "update my reservation",
    }

    def __init__(self):
        self.ai = get_ai_provider()
        self.intent_classifier = IntentClassifier()
        self.planner = Planner()
        self.memory_manager = MemoryManager()
        self.workflow = AgentWorkflow(memory_manager=self.memory_manager)
        self.view_reservation_agent = ViewReservationAgent()
        self.update_reservation_agent = UpdateReservationAgent(
            memory_manager=self.memory_manager,
        )

    async def handle(
        self,
        session_id: str,
        message: str,
        db,
    ):

        session = self.memory_manager.get_session(session_id)
        session_payload = dict(session)
        session_payload["session_id"] = session_id

        logger.info(f"SESSION ID: {session_id}")
        logger.info(f"SESSION PRE-STATE: {session}")

        if self._is_update_reservation_active(session_payload):
            return await self._update_reservation(db, session_id, message)

        if self._is_update_reservation_request(message, session_payload):
            return await self._update_reservation(db, session_id, message)

        if self._is_view_reservation_request(message, session_payload):
            return await self._view_reservations(db)

        if session_payload.get("intent"):
            intent = session_payload["intent"]
            confidence = session_payload.get("intent_confidence", 0.0)
        else:
            intent_result = await self.intent_classifier.classify(message)
            intent = intent_result.get("intent", "general")
            confidence = intent_result.get("confidence", 0.0)

            if intent == "view_reservation":
                return await self._view_reservations(db)

            if intent == "update_reservation":
                return await self._update_reservation(db, session_id, message)

            self.memory_manager.update_session(
                session_id,
                {
                    "intent": intent,
                    "intent_confidence": confidence,
                }
            )

        logger.info(f"MEMORY: {self.memory_manager.get_session(session_id)}")

        if intent in {"reservation", "check_reservation", "cancel_reservation", "greeting", "general_question"}:
            session = self.memory_manager.get_session(session_id)
            session_payload = dict(session)
            session_payload["session_id"] = session_id
            plan = await self.planner.plan(
                intent_result={
                    "intent": intent,
                    "confidence": confidence,
                },
                conversation_state=session_payload,
            )

            workflow_result = await self.workflow.execute(
                plan,
                session_payload,
                message,
                session_id=session_id,
            )

            response_text = workflow_result.get("response", "")

            if workflow_result.get("status") == "complete":
                self.memory_manager.update_session(
                    session_id,
                    {"completed": True},
                )
            elif workflow_result.get("status") == "awaiting_input":
                self.memory_manager.update_session(
                    session_id,
                    {
                        workflow_result.get("field"): None,
                    },
                )

            return response_text

        return await self.ai.chat(message)

    def _is_view_reservation_request(
        self,
        message: str,
        session_state: dict,
    ) -> bool:
        if session_state.get("awaiting_confirmation"):
            return False

        normalized_message = " ".join(message.lower().strip().split())
        return normalized_message in self.VIEW_RESERVATION_PHRASES

    def _is_update_reservation_active(self, session_state: dict) -> bool:
        return (
            not session_state.get("awaiting_confirmation")
            and bool(session_state.get("update_reservation_stage"))
        )

    def _is_update_reservation_request(
        self,
        message: str,
        session_state: dict,
    ) -> bool:
        if session_state.get("awaiting_confirmation"):
            return False

        normalized_message = " ".join(message.lower().strip().split())
        return normalized_message in self.UPDATE_RESERVATION_PHRASES

    async def _view_reservations(self, db) -> str:
        result = await self.view_reservation_agent.run(db)
        return result.get("response", "")

    async def _update_reservation(self, db, session_id: str, message: str) -> str:
        result = await self.update_reservation_agent.run(db, session_id, message)
        session = self.memory_manager.get_session(session_id)
        logger.info(
            "UPDATE RESERVATION STATE: session_id=%s status=%s "
            "reservation_id=%s stage=%s editing_field=%s",
            session_id,
            result.get("status"),
            session.get("reservation_id"),
            session.get("update_reservation_stage"),
            session.get("editing_field"),
        )
        return result.get("response", "")
