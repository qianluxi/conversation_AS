from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from ..config import MockSettings
from .base import AgentAdapter, AgentReply, HealthStatus, MessageRef


class MockAdapter(AgentAdapter):
    """离线演示/测试用适配器，不需要任何凭据。"""

    kind = "mock"

    def __init__(self, name: str, settings: MockSettings) -> None:
        super().__init__(name)
        self.role = settings.role
        self.delay = settings.delay
        self._replies: dict[str, AgentReply] = {}
        self._sessions: dict[str, list[str]] = {}

    async def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail=f"mock({self.role})")

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        session_id = f"mock-{uuid4().hex[:8]}"
        self._sessions[session_id] = []
        return session_id

    def _compose(self, text: str) -> str:
        if self.role in ("planner", "reviewer"):
            if "你是审查者" in text or "【执行结果】" in text:
                result = text.split("【执行结果】")[-1]
                approved = "APPROVED" in result.upper() or "执行完成" in result
                payload = {
                    "decision": "approved" if approved else "revision_required",
                    "summary": "mock 审查完成",
                    "findings": [] if approved else [{
                        "severity": "medium",
                        "file": None,
                        "location": None,
                        "problem": "mock：需要补充说明",
                        "suggested_action": "补充异常值处理说明",
                    }],
                    "evidence": ["mock-review"],
                    "confidence": 0.9,
                }
                return json.dumps(payload, ensure_ascii=False)
            return (
                "分析完成，形成任务方案：\n"
                f"- 目标：{text.strip()[:200]}\n"
                "- 要求：检查数据泄漏、时间序列划分、异常值处理\n"
                "- 期望输出：problem_list / severity / file_location / suggested_fix"
            )
        if self.role == "worker":
            return f"执行完成：已根据任务方案完成检查与修改。\n原始任务摘要：{text.strip()[:200]}"
        return f"[{self.name}] {text}"

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        if self.delay:
            await asyncio.sleep(self.delay)
        message_id = f"mockmsg-{uuid4().hex[:8]}"
        self._sessions.setdefault(session_id, []).append(text)
        self._replies[message_id] = AgentReply(
            session_id=session_id,
            text=self._compose(text),
            provider_message_id=message_id,
            stop_reason="end_turn",
            metadata={"mock": True, "role": self.role},
        )
        return MessageRef(session_id=session_id, message_id=message_id)

    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        return self._replies.pop(ref.message_id)

    async def cancel(self, session_id: str) -> None:
        return None
