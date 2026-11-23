from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class AdapterError(RuntimeError):
    """适配器基类错误。"""


class AdapterAuthError(AdapterError):
    """鉴权失败或缺少凭据。"""


class AdapterTimeout(AdapterError):
    """等待响应超时。"""


class AdapterRemoteError(AdapterError):
    """远端返回错误。"""

    def __init__(self, message: str, *, status_code: int | None = None,
                 payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class HealthStatus:
    ok: bool
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageRef:
    session_id: str
    message_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentReply:
    session_id: str
    text: str
    provider_message_id: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(ABC):
    """统一适配器接口。上层工作流只依赖这些方法。"""

    kind: str = "base"

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        ...

    @abstractmethod
    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        """创建/获取会话，返回适配器内部使用的会话 ID。"""

    @abstractmethod
    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        """提交一条消息，尽快返回引用；实际响应可能稍后才产生。"""

    @abstractmethod
    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        """等待并返回完整响应。"""

    @abstractmethod
    async def cancel(self, session_id: str) -> None:
        ...

    async def recover_reply(self, session_id: str, ref: MessageRef) -> AgentReply | None:
        """恢复时查询外部会话，判断某条消息是否已经产生回复。

        返回 None 表示无法确认（调用方必须保持 UNKNOWN，不能盲目重发）。
        """
        return None

    async def cancel_wait(self, session_id: str) -> None:
        """只停止本地等待，不保证远端停止（默认复用 cancel）。"""
        await self.cancel(session_id)

    async def remote_abort(self, session_id: str) -> None:
        """请求服务端真正中止执行；不支持的适配器应抛出 AdapterError。"""
        await self.cancel(session_id)

    async def chat(self, session_id: str, text: str, *,
                   timeout: float | None = None) -> AgentReply:
        ref = await self.send_message(session_id, text, timeout=timeout)
        return await self.wait_response(ref, timeout=timeout)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> "AgentAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


async def sleep_or_cancel(seconds: float) -> None:
    await asyncio.sleep(seconds)
