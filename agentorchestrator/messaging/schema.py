from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class MessageStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Message(BaseModel):
    """协调器统一消息格式，不依赖任一智能体的原始聊天结构。"""

    message_id: str = Field(default_factory=lambda: new_id("msg"))
    run_id: str
    turn_id: int = 0
    from_agent: str
    to_agent: str
    content: str
    status: MessageStatus = MessageStatus.PENDING
    provider: str | None = None
    provider_session_id: str | None = None
    provider_message_id: str | None = None
    reply_to: str | None = None
    dedupe_key: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    goal: str = ""
    status: RunStatus = RunStatus.RUNNING
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionBinding(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    run_id: str
    agent: str
    provider: str
    provider_session_id: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
