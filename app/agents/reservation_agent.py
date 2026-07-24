import re
from typing import Any

from app.brain.context_resolver import ContextResolver
from app.brain.conversation_state_manager import ConversationStateManager
from app.brain.memory_manager import MemoryManager
from app.brain.reservation_entity_extractor import (
    ReservationEntityExtractor,
    normalize_natural_reservation_name,
)
from app.core.input_validation import (
    InputValidationError,
    validate_reservation_field,
)
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
        db=None,
    ) -> dict[str, Any]:
        current_session_id = session_id or str(session_state.get("session_id") or "default")
        if session_state.get("awaiting_confirmation"):
            return await self.handle_confirmation(
                user_message,
                current_session_id,
                owner_customer_id=owner_customer_id,
                db=db,
            )

        step = steps[0] if steps else None
        if step is None:
            return {
                "status": "completed",
                "response": "Tidak ada langkah yang tersedia.",
            }

        extracted = await self.entity_extractor.extract(user_message)
        pending_field = self._infer_pending_field(session_state)
        candidates = dict(extracted or {})
        pending_value_invalid = False
        if pending_field and not candidates.get(pending_field):
            inferred_value = self._infer_value_for_field(pending_field, user_message)
            if inferred_value is not None:
                candidates[pending_field] = inferred_value
            else:
                pending_value_invalid = True

        updates = {}
        invalid_fields = set()
        for key, value in candidates.items():
            if key not in self.EDITABLE_FIELDS or value is None:
                continue
            normalized_value = self._normalize_and_validate_field(key, value)
            if normalized_value is None:
                invalid_fields.add(key)
                continue
            updates[key] = normalized_value
        if updates:
            session_state = dict(session_state)
            resolved_state = self.context_resolver.resolve(session_state, user_message, updates)
            self.memory_manager.update_session(current_session_id, resolved_state)
            session_state = resolved_state

        if session_state.get("user_id"):
            preferences = self.long_term_memory.suggest_context(session_state["user_id"])
            if preferences.get("favorite_name") and not session_state.get("name"):
                preferred_name = self._normalize_and_validate_field(
                    "name",
                    preferences["favorite_name"],
                )
                if preferred_name is not None:
                    session_state["name"] = preferred_name
            if preferences.get("preferred_people") and not session_state.get("people"):
                preferred_people = self._normalize_and_validate_field(
                    "people",
                    preferences["preferred_people"],
                )
                if preferred_people is not None:
                    session_state["people"] = preferred_people
            if preferences.get("favorite_time") and not session_state.get("time"):
                favorite_time = self._normalize_and_validate_field(
                    "time",
                    preferences["favorite_time"],
                )
                if favorite_time is not None:
                    session_state["time"] = favorite_time

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
            if pending_field and (
                pending_value_invalid or pending_field in invalid_fields
            ):
                return {
                    "status": "awaiting_input",
                    "response": self._question_for_field(pending_field),
                    "field": pending_field,
                    "next_action": f"ask_{pending_field}",
                    "invalid_input": True,
                }
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
        db=None,
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
            if db is None:
                return {
                    "status": "awaiting_confirmation",
                    "response": "Layanan reservasi belum tersedia. Silakan coba lagi.",
                }

            canonical_values = {}
            for field_name in self.EDITABLE_FIELDS:
                canonical_value = self._normalize_and_validate_field(
                    field_name,
                    session.get(field_name),
                )
                if canonical_value is None:
                    self.memory_manager.update_session(
                        session_id,
                        {
                            "awaiting_confirmation": True,
                            "editing_field": field_name,
                        },
                    )
                    return {
                        "status": "awaiting_confirmation",
                        "response": self._question_for_edit_field(field_name),
                        "invalid_input": True,
                    }
                canonical_values[field_name] = canonical_value
            self.memory_manager.update_session(session_id, canonical_values)
            reservation_data = ReservationCreate(**canonical_values)
            reservation = self.reservation_service.create_reservation(
                db,
                reservation_data,
                owner_customer_id=owner_customer_id,
            )
            reservation_id = reservation.id
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
            return self._parse_people_candidate(user_message)

        if field_name == "date":
            match = re.search(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b", user_message)
            if match:
                return self._normalize_and_validate_field(
                    field_name,
                    match.group(0),
                )
            parsed_date = DatetimeParser.parse_date(user_message)
            if parsed_date:
                return self._normalize_and_validate_field(field_name, parsed_date)
            return None

        if field_name == "time":
            match = re.search(r"\b(?:[01][0-9]|2[0-3]):[0-5][0-9]\b", user_message)
            if match:
                return self._normalize_and_validate_field(
                    field_name,
                    match.group(0),
                )
            parsed_time = DatetimeParser.parse_time(user_message)
            if parsed_time:
                return self._normalize_and_validate_field(field_name, parsed_time)
            return None

        return None

    def _extract_direct_name_value(self, user_message: str) -> str | None:
        patterns = (
            r"(?:ubah|ganti|edit|perbaiki|koreksi)\s+(?:nama|atas nama)(?:\s+(?:menjadi|jadi|ke))?\s+(.+)$",
            r"(?:nama|atas nama)\s+(?:menjadi|jadi|ke)\s+(.+)$",
        )

        for pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                try:
                    return normalize_natural_reservation_name(match.group(1))
                except InputValidationError:
                    return None

        return None

    def _normalize_edit_value(self, field_name: str, value: Any) -> Any:
        return self._normalize_and_validate_field(field_name, value)

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
        text = user_message
        if field_name == "name":
            try:
                return normalize_natural_reservation_name(text)
            except InputValidationError:
                return None
        if field_name == "people":
            return self._parse_people_candidate(text)
        if field_name == "date":
            return self._normalize_and_validate_field(field_name, text)
        if field_name == "time":
            return self._normalize_and_validate_field(field_name, text)
        return None

    def _normalize_and_validate_field(self, field_name: str, value: Any) -> Any:
        candidate = value
        if field_name == "date" and isinstance(value, str):
            candidate = DatetimeParser.parse_date(value) or value
        elif field_name == "time" and isinstance(value, str):
            candidate = DatetimeParser.parse_time(value) or value
        try:
            return validate_reservation_field(field_name, candidate)
        except InputValidationError:
            return None

    def _parse_people_candidate(self, text: str) -> int | None:
        if re.search(r"(?<![0-9])-[ ]*[0-9]+", text):
            return None
        if re.search(r"[0-9]+\.[0-9]+", text):
            return None
        values = re.findall(r"(?<![0-9.])[0-9]+(?![0-9.])", text)
        if len(values) != 1:
            return None
        return self._normalize_and_validate_field("people", int(values[0]))

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
