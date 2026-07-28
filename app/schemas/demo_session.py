"""Strict server-to-server DTOs for the internal portfolio demo session API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _InternalDemoDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class DemoSessionSummary(_InternalDemoDTO):
    status: Literal["active"] = "active"
    expires_at: datetime = Field(serialization_alias="expiresAt")
    idle_expires_at: datetime = Field(serialization_alias="idleExpiresAt")
    absolute_expires_at: datetime = Field(
        serialization_alias="absoluteExpiresAt"
    )
    message_count: int = Field(ge=0, serialization_alias="messageCount")


class DemoSessionMessage(_InternalDemoDTO):
    id: int = Field(gt=0)
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(serialization_alias="createdAt")


class DemoSessionHandoff(_InternalDemoDTO):
    reference: str
    status: Literal["simulated"]
    reason_code: str = Field(serialization_alias="reasonCode")
    safe_summary: str | None = Field(serialization_alias="safeSummary")
    created_at: datetime = Field(serialization_alias="createdAt")


class DemoSessionCreateResponse(_InternalDemoDTO):
    session_token: str = Field(serialization_alias="sessionToken")
    session: DemoSessionSummary


class DemoSessionCurrentResponse(_InternalDemoDTO):
    session: DemoSessionSummary
    messages: tuple[DemoSessionMessage, ...]
    handoff: DemoSessionHandoff | None
