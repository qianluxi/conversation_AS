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
    CREATED = "created"
    SENDING = "sending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    # 兼容旧数据，新代码不再写入
    PENDING = "pending"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunPhase(str, Enum):
    CREATED = "created"
    ANALYZING = "analyzing"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    REVISION_REQUIRED = "revision_required"
    APPROVED = "approved"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Message(BaseModel):
    """协调器统一消息格式，不依赖任一智能体的原始聊天结构。"""

    message_id: str = Field(default_factory=lambda: new_id("msg"))
    run_id: str
    turn_id: int = 0
    from_agent: str
    to_agent: str
    content: str
    status: MessageStatus = MessageStatus.CREATED
    provider: str | None = None
    provider_session_id: str | None = None
    provider_message_id: str | None = None
    reply_to: str | None = None
    dedupe_key: str | None = None
    attempt: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_attempt_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    goal: str = ""
    status: RunStatus = RunStatus.RUNNING
    phase: RunPhase = RunPhase.CREATED
    current_turn: int = 0
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


class Step(BaseModel):
    """一次节点执行（某个 agent 在某一轮的工作）。"""

    step_id: str = Field(default_factory=lambda: new_id("step"))
    run_id: str
    turn_id: int = 0
    agent: str
    role: str = "worker"
    state: StepState = StepState.PENDING
    attempt: int = 1
    input_message_id: str | None = None
    output_message_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    seq: int = 0
    event_type: str
    phase: RunPhase
    step_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
