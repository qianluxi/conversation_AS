from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from ..auth import WorkBuddyAuth
from ..config import WorkBuddyLocalSettings
from ..locks import CrossProcessFileLock, CrossProcessLockError
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRemoteError,
    AdapterTimeout,
    AdapterUncertain,
    AgentAdapter,
    AgentReply,
    HealthStatus,
    MessageRef,
    translate_transport_error,
)

log = logging.getLogger(__name__)


class LocalAssistantPhase(str, Enum):
    IDLE = "idle"
    SUBMITTED = "submitted"
    RUNNING = "running"
    QUARANTINED = "quarantined"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LocalAssistantGate:
    """同一 PC 本地助理的远端执行状态。

    IDLE -> SUBMITTED -> RUNNING -> IDLE
                          └──────> QUARANTINED（超时/断线/取消等待）
    QUARANTINED 只能通过确认远端历史或人工强制解除，期间禁止新消息。
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    phase: LocalAssistantPhase = LocalAssistantPhase.IDLE
    message_id: str | None = None
    owner: str | None = None
    reason: str = ""
    updated_at: str = field(default_factory=_now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "message_id": self.message_id,
            "owner": self.owner,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }


class WorkBuddyLocalAdapter(AgentAdapter):
    """WorkBuddy 本地助理 API（OAuth2.1）适配器。

    本地助理没有独立 session，也没有远端取消接口，因此：
    - 同一 base_url 的所有适配器实例共享一个 LocalAssistantGate；
    - 等待超时/断线后进入 QUARANTINED，不释放远端占用，直到核对历史或人工释放。
    """

    kind = "workbuddy_local"
    session_placeholder = "workbuddy-local"

    def __init__(self, name: str, settings: WorkBuddyLocalSettings,
                 auth: WorkBuddyAuth | None = None,
                 client: httpx.AsyncClient | None = None,
                 gate: LocalAssistantGate | None = None,
                 lock: asyncio.Lock | None = None) -> None:
        super().__init__(name)
        self.settings = settings
        self._base = settings.base_url.rstrip("/")
        self._auth = auth or WorkBuddyAuth(
            base_url=settings.base_url, client_id=settings.client_id,
            client_secret=settings.client_secret, redirect_uri=settings.redirect_uri,
            token_file=settings.token_file, scope=settings.scope,
        )
        self._client = client
        self._owns_client = client is None
        if gate is None:
            gate = LocalAssistantGate(lock=lock or asyncio.Lock())
        self._gate = gate
        self._file_lock = CrossProcessFileLock(settings.lock_file)

    # ---- http -------------------------------------------------------------

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self.settings.verify_ssl,
                timeout=httpx.Timeout(self.settings.timeout, connect=10.0),
            )
        return self._client

    async def _request(self, method: str, path: str, *, timeout: float | None = None,
                       retry_auth: bool = True, **kwargs: Any) -> dict[str, Any]:
        token = await self._auth.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        client = await self._http()
        try:
            response = await client.request(
                method, f"{self._base}{path}", headers=headers,
                timeout=timeout if timeout is not None else self.settings.timeout, **kwargs,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise AdapterError(f"无法连接 WorkBuddy Open API（{self._base}）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, what="WorkBuddy 本地助理") from exc

        if response.status_code in (401, 403):
            if retry_auth:
                await self._auth.refresh()
                return await self._request(method, path, timeout=timeout,
                                           retry_auth=False, **kwargs)
            raise AdapterAuthError("WorkBuddy 鉴权失败，请重新执行 OAuth 授权。")
        if response.status_code >= 500:
            raise AdapterUncertain(
                f"WorkBuddy 返回 {response.status_code}（请求结果不确定）：{response.text[:300]}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise AdapterRemoteError(
                f"WorkBuddy 返回 {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        payload = response.json()
        if int(payload.get("code", 0)) != 0:
            raise AdapterRemoteError(
                f"WorkBuddy 业务错误 {payload.get('code')}: {payload.get('msg')}", payload=payload
            )
        return payload

    # ---- gate helpers -----------------------------------------------------

    def _set_gate(self, phase: LocalAssistantPhase, *, message_id: str | None = None,
                  reason: str = "") -> None:
        self._gate.phase = phase
        self._gate.message_id = message_id
        self._gate.reason = reason
        self._gate.owner = self.name if phase is not LocalAssistantPhase.IDLE else None
        self._gate.updated_at = _now()

    async def _mark(self, phase: LocalAssistantPhase, *, message_id: str | None = None,
                    reason: str = "") -> None:
        async with self._gate.lock:
            self._set_gate(phase, message_id=message_id, reason=reason)

    def state(self) -> dict[str, Any]:
        return self._gate.snapshot()

    # ---- health / session -------------------------------------------------

    async def health_check(self) -> HealthStatus:
        payload = await self._request("GET", "/openapi/v2/localassistant", timeout=15)
        online = bool((payload.get("data") or {}).get("online"))
        return HealthStatus(ok=online, detail="WorkBuddy PC 端本地助理在线" if online else "本地助理离线",
                            metadata={"request_id": payload.get("request_id"),
                                      "gate": self._gate.snapshot()})

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        # 本地助理没有 session_id，使用固定值；协调器仍按 run 记录映射。
        return self.session_placeholder

    # ---- send / wait ------------------------------------------------------

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        async with self._gate.lock:
            if self._gate.phase is LocalAssistantPhase.QUARANTINED:
                raise AdapterError(
                    "WorkBuddy 本地助理处于隔离状态："
                    f"{self._gate.reason or '上一轮结果未知'}。"
                    "请先运行 `agentorchestrator resume` 或 `release` 确认远端状态。"
                )
            if self._gate.phase is not LocalAssistantPhase.IDLE:
                raise AdapterError(
                    f"WorkBuddy 本地助理正在处理 {self._gate.message_id or '上一轮任务'}"
                    f"（{self._gate.phase.value}），禁止并发发送。"
                )
            try:
                self._file_lock.acquire()
            except CrossProcessLockError as exc:
                raise AdapterError(str(exc)) from exc
            self._set_gate(LocalAssistantPhase.SUBMITTED)

        try:
            payload = await self._request(
                "POST", "/openapi/v2/localassistant/message",
                json={"content": text, "msg_type": "text"},
            )
            message_id = (payload.get("data") or {}).get("message_id")
            if not message_id:
                raise AdapterRemoteError("WorkBuddy 未返回 message_id", payload=payload)
        except (AdapterUncertain, AdapterTimeout) as exc:
            await self._mark(LocalAssistantPhase.QUARANTINED, reason=str(exc))
            raise
        except BaseException:
            self._file_lock.release()
            await self._mark(LocalAssistantPhase.IDLE)
            raise

        await self._mark(LocalAssistantPhase.RUNNING, message_id=message_id)
        return MessageRef(session_id=session_id, message_id=message_id,
                          metadata={"poll_from": message_id})

    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        limit = timeout if timeout is not None else self.settings.timeout
        deadline = asyncio.get_running_loop().time() + limit
        last_text = ""
        try:
            while True:
                payload = await self._request(
                    "GET", "/openapi/v2/localassistant/message",
                    params={"message_id": ref.message_id}, timeout=15,
                )
                messages = (payload.get("data") or {}).get("messages") or []
                assistant = [item for item in messages if item.get("role") == "assistant"]
                if assistant:
                    parts: list[str] = []
                    for item in assistant:
                        content = item.get("content") or []
                        if isinstance(content, str):
                            parts.append(content)
                        else:
                            parts.extend(str(piece) for piece in content)
                    last_text = "\n".join(part for part in parts if part).strip()
                    self._file_lock.release()
                    await self._mark(LocalAssistantPhase.IDLE)
                    return AgentReply(
                        session_id=ref.session_id,
                        text=last_text,
                        provider_message_id=assistant[-1].get("message_id"),
                        stop_reason="end_turn",
                        metadata={"messages": assistant},
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._mark(
                        LocalAssistantPhase.QUARANTINED,
                        message_id=ref.message_id,
                        reason=f"等待响应超时（{limit}s），远端可能仍在执行",
                    )
                    raise AdapterTimeout(
                        f"等待 WorkBuddy 本地助理响应超时（{limit}s）。已收到：{last_text[:300]}"
                    )
                await asyncio.sleep(min(self.settings.poll_interval, remaining))
        except (AdapterTimeout, AdapterUncertain):
            raise
        except AdapterError as exc:
            await self._mark(LocalAssistantPhase.QUARANTINED,
                             message_id=ref.message_id, reason=str(exc))
            raise

    async def recover_reply(self, session_id: str, ref: MessageRef) -> AgentReply | None:
        """查询本地助理历史，判断该消息是否已经产生回复；有回复则解除隔离。"""
        payload = await self._request(
            "GET", "/openapi/v2/localassistant/message",
            params={"message_id": ref.message_id}, timeout=15,
        )
        messages = (payload.get("data") or {}).get("messages") or []
        assistant = [item for item in messages if item.get("role") == "assistant"]
        if not assistant:
            if self._gate.phase is not LocalAssistantPhase.IDLE:
                await self._mark(LocalAssistantPhase.QUARANTINED,
                                 message_id=ref.message_id,
                                 reason="远端历史中未找到回复，禁止新消息")
            return None
        parts: list[str] = []
        for item in assistant:
            content = item.get("content") or []
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.extend(str(piece) for piece in content)
        self._file_lock.release()
        await self._mark(LocalAssistantPhase.IDLE)
        return AgentReply(
            session_id=session_id, text="\n".join(part for part in parts if part).strip(),
            provider_message_id=assistant[-1].get("message_id"),
            stop_reason="end_turn", metadata={"mode": "recovered", "messages": assistant},
        )

    # ---- control ----------------------------------------------------------

    async def release_quarantine(self, *, force: bool = False,
                                 message_id: str | None = None) -> bool:
        """尝试解除隔离：先查远端历史，force=True 时人工强制解除。"""
        if self._gate.phase is LocalAssistantPhase.IDLE:
            return True
        target = message_id or self._gate.message_id
        if target and (self._gate.message_id in (None, target) or force):
            reply = await self.recover_reply(
                self.session_placeholder,
                MessageRef(session_id=self.session_placeholder, message_id=target),
            )
            if reply is not None:
                return True
        if not force:
            raise AdapterError(
                "无法确认 WorkBuddy 远端任务是否结束。请先在 WorkBuddy 桌面端确认，"
                "必要时使用 --force 强制解除隔离（可能导致上一轮回复丢失）。"
            )
        await self._mark(LocalAssistantPhase.IDLE)
        self._file_lock.release()
        log.warning("已人工强制解除 WorkBuddy 本地助理隔离。")
        return True

    async def cancel_wait(self, session_id: str) -> None:
        """只停止本地等待；远端占用保留为 QUARANTINED。"""
        if self._gate.phase is not LocalAssistantPhase.IDLE:
            await self._mark(LocalAssistantPhase.QUARANTINED,
                             message_id=self._gate.message_id,
                             reason="本地已停止等待，远端可能仍在执行")
        log.warning("停止等待 WorkBuddy 本地助理响应；远端任务可能仍在执行。")

    async def remote_abort(self, session_id: str) -> None:
        raise AdapterError("WorkBuddy 本地助理 API 不提供远端取消接口（remote_abort 不可用）。")

    async def cancel(self, session_id: str) -> None:
        await self.cancel_wait(session_id)

    async def aclose(self) -> None:
        self._file_lock.release()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        await self._auth.aclose()
