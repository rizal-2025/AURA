import re
import uuid
from typing import Any

from app.brain.context_resolver import ContextResolver
from app.brain.conversation_state_manager import ConversationStateManager
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_entity_extractor import ReservationEntityExtractor
from app.db.database import SessionLocal
from app.memory.long_term_memory import LongTermMemoryManager
from app.schemas.reservation import ReservationCreate
from app.services.reservation.service import ReservationService
from app.utils.datetime_parser import DatetimeParser


CONFIRM = "CONFIRM"
REJECT = "REJECT"
EDIT_FIELD = "EDIT_FIELD"


class ReservationAgent:
    """Handle reservation-related workflow steps."""

    EDITABLE_FIELDS = ("name", "people", "date", "time")
    POSITIVE_CONFIRMATION_ANSWERS = {
        "ya",
        "iya",
        "yes",
        "benar",
        "betul",
        "oke",
        "ok",
        "okay",
    }
    NEGATIVE_CONFIRMATION_ANSWERS = {
        "tidak",
        "bukan",
        "salah",
        "no",
        "nope",
        "nggak",
        "gak",
    }

    def __init__(self, memory_manager: MemoryManager | None = None):
        self.memory_manager = memory_manager or MemoryManager()
        self.entity_extractor = ReservationEntityExtractor()
        self.reservation_service = ReservationService()
        self.conversation_state_manager = ConversationStateManager()
        self.context_resolver = ContextResolver()
        self.long_term_memory = LongTermMemoryManager()

    async def run(
        self,
        steps: list[dict[str, Any]],
        session_state: dict[str, Any],
        user_message: str,
        session_id: str | None = None,
        owner_customer_id=None,
    ) -> dict[str, Any]:
        current_session_id = session_id or str(session_state.get("session_id") or "default")
        if session_state.get("awaiting_confirmation"):
            return await self.handle_confirmation(
                user_message,
                current_session_id,
                owner_customer_id=owner_customer_id,
            )

        step = steps[0] if steps else None
        if step is None:
            return {
                "status": "completed",
                "response": "Tidak ada langkah yang tersedia.",
            }

        extracted = await self.entity_extractor.extract(user_message)
        pending_field = self._infer_pending_field(session_state)
        updates = dict(extracted or {})
        if pending_field and not updates.get(pending_field):
            inferred_value = self._infer_value_for_field(pending_field, user_message)
            if inferred_value is not None:
                updates[pending_field] = inferred_value

        if updates:
            session_state = dict(session_state)
            resolved_state = dict(session_state)
            for key, value in updates.items():
                if value is not None:
                    normalized_value = value
                    if key == "date":
                        normalized_value = DatetimeParser.parse_date(str(value)) or value
                    elif key == "time":
                        normalized_value = DatetimeParser.parse_time(str(value)) or value
                    updates[key] = normalized_value
            resolved_state = self.context_resolver.resolve(session_state, user_message, updates)
            self.memory_manager.update_session(current_session_id, resolved_state)
            session_state = resolved_state

        if session_state.get("user_id"):
            preferences = self.long_term_memory.suggest_context(session_state["user_id"])
            if preferences.get("favorite_name") and not session_state.get("name"):
                session_state["name"] = preferences["favorite_name"]
            if preferences.get("preferred_people") and not session_state.get("people"):
                session_state["people"] = preferences["preferred_people"]
            if preferences.get("favorite_time") and not session_state.get("time"):
                session_state["time"] = preferences["favorite_time"]

            profile_updates = {}
            if session_state.get("name"):
                profile_updates["favorite_name"] = session_state["name"]
            if session_state.get("people"):
                profile_updates["preferred_people"] = session_state["people"]
            if session_state.get("time"):
                profile_updates["favorite_time"] = session_state["time"]
            if session_state.get("table"):
                profile_updates["favorite_table"] = session_state["table"]
            if profile_updates:
                self.long_term_memory.merge_preferences(session_state["user_id"], profile_updates)

        action = step.get("action")
        if action == "collect_missing_fields":
            reasoning_state = dict(session_state)
            next_action = self.conversation_state_manager.get_next_action(reasoning_state)
            if next_action["next_action"] == "confirm":
                self.memory_manager.update_session(current_session_id, {
                    "awaiting_confirmation": True,
                })
                return {
                    "status": "awaiting_confirmation",
                    "response": self._confirmation_message(session_state),
                }

            if next_action["next_action"] == "complete":
                return {
                    "status": "completed",
                    "response": "Reservasi sudah lengkap.",
                }

            field = next_action["field"]
            if field:
                reasoning_state = self.conversation_state_manager.record_question(reasoning_state, field)
                self.memory_manager.update_session(current_session_id, reasoning_state)

            return {
                "status": "awaiting_input",
                "response": next_action["question"],
                "field": field,
                "next_action": next_action["next_action"],
            }

        if action == "save_reservation":
            return {
                "status": "complete",
                "response": "Reservasi siap disimpan.",
                "reservation": {
                    "name": session_state.get("name"),
                    "people": session_state.get("people"),
                    "date": session_state.get("date"),
                    "time": session_state.get("time"),
                },
            }

        return {
            "status": "unknown_action",
            "response": f"Langkah tidak dikenal: {action}",
        }

    async def handle_confirmation(
        self,
        user_message: str,
        session_id: str,
        owner_customer_id=None,
    ) -> dict[str, Any]:
        session = self.memory_manager.get_session(session_id)
        editing_field = session.get("editing_field")

        if editing_field in self.EDITABLE_FIELDS:
            value = self._infer_value_for_field(editing_field, user_message)
            if value is not None:
                updated_session = self._apply_confirmation_edit(
                    session_id,
                    editing_field,
                    value,
                )
                return self._confirmation_response(updated_session)

            return {
                "status": "awaiting_confirmation",
                "response": self._question_for_edit_field(editing_field),
            }

        intent, field = self._detect_confirmation_intent(user_message)

        if intent == CONFIRM:
            if owner_customer_id is None:
                return {
                    "status": "awaiting_confirmation",
                    "response": "Identitas pelanggan tidak tersedia. Silakan coba lagi.",
                }

            reservation_data = ReservationCreate(
                name=session.get("name"),
                people=session.get("people"),
                date=session.get("date"),
                time=session.get("time"),
            )
            db = SessionLocal()
            try:
                reservation = self.reservation_service.create_reservation(
                    db,
                    reservation_data,
                    owner_customer_id=owner_customer_id,
                )
            finally:
                db.close()

            reservation_id = str(uuid.uuid4())
            self.memory_manager.update_session(session_id, {
                "completed": True,
                "awaiting_confirmation": False,
                "reservation_id": reservation_id,
            })
            self.memory_manager.get_session(session_id)["editing_field"] = None
            return {
                "status": "completed",
                "response": (
                    "Reservasi berhasil dibuat.\n\n"
                    f"Nomor reservasi: {reservation_id}\n\n"
                    "Sampai jumpa."
                ),
            }

        if intent == REJECT:
            self.memory_manager.update_session(session_id, {
                "awaiting_confirmation": True,
            })
            self.memory_manager.get_session(session_id)["editing_field"] = None
            return {
                "status": "rejected",
                "response": "Silakan kirim data yang ingin diperbaiki. Field mana yang ingin diperbaiki?",
            }

        if intent == EDIT_FIELD and field:
            value = await self._extract_direct_edit_value(field, user_message)
            if value is not None:
                updated_session = self._apply_confirmation_edit(session_id, field, value)
                return self._confirmation_response(updated_session)

            self.memory_manager.update_session(session_id, {
                "awaiting_confirmation": True,
                "editing_field": field,
            })
            return {
                "status": "awaiting_confirmation",
                "response": self._question_for_edit_field(field),
            }

        return {
            "status": "awaiting_confirmation",
            "response": self._confirmation_message(session),
            "invalid_input": True,
        }

    def _detect_confirmation_intent(self, user_message: str) -> tuple[str | None, str | None]:
        normalized = user_message.strip().lower()

        if normalized in self.POSITIVE_CONFIRMATION_ANSWERS:
            return CONFIRM, None

        if normalized in self.NEGATIVE_CONFIRMATION_ANSWERS:
            return REJECT, None

        if not any(keyword in normalized for keyword in ("ubah", "ganti", "edit", "perbaiki", "koreksi")):
            return None, None

        return EDIT_FIELD, self._detect_edit_field(normalized)

    def _detect_edit_field(self, user_message: str) -> str | None:
        aliases = {
            "name": ("atas nama", "nama", "name"),
            "people": ("jumlah orang", "jumlah", "orang", "people"),
            "date": ("tanggal", "hari", "date"),
            "time": ("jam", "pukul", "waktu", "time"),
        }

        for field in self.EDITABLE_FIELDS:
            if any(alias in user_message for alias in aliases[field]):
                return field

        return None

    async def _extract_direct_edit_value(self, field_name: str, user_message: str) -> Any:
        extracted = await self.entity_extractor.extract(user_message)
        extracted_value = extracted.get(field_name)
        if extracted_value is not None:
            return self._normalize_edit_value(field_name, extracted_value)

        if field_name == "name":
            return self._extract_direct_name_value(user_message)

        if field_name == "people":
            match = re.search(r"(?:menjadi|jadi|ke)\s*(\d+)\b", user_message.lower())
            if match:
                return int(match.group(1))
            return None

        if field_name == "date":
            parsed_date = DatetimeParser.parse_date(user_message)
            if parsed_date:
                return parsed_date

            match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", user_message)
            return match.group(0) if match else None

        if field_name == "time":
            parsed_time = DatetimeParser.parse_time(user_message)
            if parsed_time:
                return parsed_time

            match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", user_message)
            return match.group(0) if match else None

        return None

    def _extract_direct_name_value(self, user_message: str) -> str | None:
        patterns = (
            r"(?:ubah|ganti|edit|perbaiki|koreksi)\s+(?:nama|atas nama)(?:\s+(?:menjadi|jadi|ke))?\s+(.+)$",
            r"(?:nama|atas nama)\s+(?:menjadi|jadi|ke)\s+(.+)$",
        )

        for pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                value = match.group(1).strip(" .,!?:;")
                return value.title() if value else None

        return None

    def _normalize_edit_value(self, field_name: str, value: Any) -> Any:
        if field_name == "date":
            return DatetimeParser.parse_date(str(value)) or value
        if field_name == "time":
            return DatetimeParser.parse_time(str(value)) or value
        return value

    def _apply_confirmation_edit(self, session_id: str, field_name: str, value: Any) -> dict[str, Any]:
        self.memory_manager.update_session(session_id, {field_name: value})
        updated_session = self.memory_manager.get_session(session_id)
        updated_session["editing_field"] = None
        updated_session["awaiting_confirmation"] = True
        return updated_session

    def _confirmation_response(self, session_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "awaiting_confirmation",
            "response": self._confirmation_message(session_state),
        }

    def _infer_pending_field(self, session_state: dict[str, Any]) -> str | None:
        asked_fields = session_state.get("asked_fields", [])
        for field in reversed(self.conversation_state_manager.REQUIRED_FIELDS):
            if field in asked_fields and not session_state.get(field):
                return field
        return None

    def _infer_value_for_field(self, field_name: str, user_message: str) -> Any:
        text = user_message.strip()
        if field_name == "name":
            return text.title() if text else None
        if field_name == "people":
            match = re.search(r"(\d+)", text)
            return int(match.group(1)) if match else None
        if field_name == "date":
            return DatetimeParser.parse_date(text) or text
        if field_name == "time":
            return DatetimeParser.parse_time(text) or text
        return None

    def _confirmation_message(self, session_state: dict[str, Any]) -> str:
        return (
            "Baik, saya konfirmasi reservasi Anda:\n\n"
            f"Nama: {session_state.get('name', '-') }\n"
            f"Jumlah: {session_state.get('people', '-')} orang\n"
            f"Tanggal: {session_state.get('date', '-')}\n"
            f"Jam: {session_state.get('time', '-')}\n\n"
            "Apakah data ini sudah benar?\n"
            "Balas: Ya / Tidak, atau sebutkan field yang ingin diubah."
        )

    def _question_for_edit_field(self, field_name: str) -> str:
        questions = {
            "name": "Baik, nama menjadi siapa?",
            "people": "Baik, jumlah orang menjadi berapa?",
            "date": "Baik, tanggal menjadi kapan?",
            "time": "Baik, jam menjadi berapa?",
        }
        return questions.get(field_name, "Field mana yang ingin diubah?")

    def _question_for_field(self, field_name: str) -> str:
        questions = {
            "name": "Atas nama siapa reservasinya?",
            "people": "Untuk berapa orang?",
            "date": "Tanggal berapa?",
            "time": "Jam berapa?",
        }
        return questions.get(field_name, "Mohon lengkapi data reservasi.")
