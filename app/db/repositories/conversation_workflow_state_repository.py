from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.core.ownership import require_owner_customer_id
from app.db.models.conversation_workflow_state import ConversationWorkflowState


class ConversationWorkflowStateRepository:
    """Stage owner/session-scoped workflow state in caller-owned transactions."""

    def get_by_scope(
        self,
        db,
        *,
        owner_customer_id,
        session_reference_hash: str,
        for_update: bool = False,
    ):
        require_owner_customer_id(owner_customer_id)
        statement = select(ConversationWorkflowState).where(
            ConversationWorkflowState.owner_customer_id == owner_customer_id,
            ConversationWorkflowState.session_reference_hash
            == session_reference_hash,
        )
        if for_update:
            statement = statement.with_for_update()
        return db.execute(statement).scalar_one_or_none()

    def delete_by_scope(
        self,
        db,
        *,
        owner_customer_id,
        session_reference_hash: str,
    ) -> int:
        require_owner_customer_id(owner_customer_id)
        if not isinstance(session_reference_hash, str) or not session_reference_hash:
            raise ValueError("A workflow session scope is required.")
        result = db.execute(
            delete(ConversationWorkflowState).where(
                ConversationWorkflowState.owner_customer_id
                == owner_customer_id,
                ConversationWorkflowState.session_reference_hash
                == session_reference_hash,
            )
        )
        return int(result.rowcount or 0)

    def create(
        self,
        db,
        *,
        owner_customer_id,
        session_reference_hash: str,
        schema_version: int,
        payload: dict,
        is_active: bool,
    ):
        require_owner_customer_id(owner_customer_id)
        now = datetime.now(timezone.utc)
        row = ConversationWorkflowState(
            owner_customer_id=owner_customer_id,
            session_reference_hash=session_reference_hash,
            schema_version=schema_version,
            payload=payload,
            is_active=is_active,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def replace(
        row,
        *,
        schema_version: int,
        payload: dict,
        is_active: bool,
    ) -> None:
        row.schema_version = schema_version
        row.payload = payload
        row.is_active = is_active
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
