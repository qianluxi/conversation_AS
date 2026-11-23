from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from ..config import OpencodeSettings
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


@dataclass
class _Pending:
    session_id: str
    message_id: str
    baseline_assistant_ids: set[str]
    sent_at: float
    api_style: str


class OpenCodeAdapter(AgentAdapter):
    """OpenCode Adapter。

    - api_style=legacy：/session、/session/:id/prompt_async、/session/:id/message
    - api_style=v2：/api/session、/api/session/:id/prompt、/api/session/:id/wait
    - delivery=async：先提交再轮询；delivery=sync：直接阻塞等待（仅 legacy）
    """

    kind = "opencode"

    def __init__(self, name: str, settings: OpencodeSettings,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__(name)
        self.settings = settings
        self._base = settings.base_url.rstrip("/")
        self._timeout = settings.timeout
        self._client = client
        self._owns_client = client is None
        if self._client is None:
            auth = httpx.BasicAuth(settings.username, settings.password) if settings.password else None
            self._client = httpx.AsyncClient(auth=auth, verify=settings.verify_ssl,
                                             timeout=httpx.Timeout(settings.timeout, connect=10.0))
        self._pending: dict[str, _Pending] = {}
        self._replies: dict[str, AgentReply] = {}

    # ---- http helpers -----------------------------------------------------

    def _params(self) -> dict[str, str]:
        return {"directory": self.settings.directory} if self.settings.directory else {}

    async def _request(self, method: str, path: str, *, timeout: float | None = None,
                       **kwargs: Any) -> httpx.Response:
        limit = httpx.Timeout(timeout if timeout is not None else self._timeout, connect=10.0)
        try:
            response = await self._client.request(
                method, f"{self._base}{path}", timeout=limit, **kwargs
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise AdapterError(
                f"无法连接 OpenCode（{self._base}）。请确认已运行 "
                f"`opencode serve --hostname 127.0.0.1 --port 4096`。原始错误：{exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, what="OpenCode") from exc

        if response.status_code in (401, 403):
            raise AdapterAuthError(
                "OpenCode 鉴权失败。请把 OPENCODE_SERVER_PASSWORD 写入配置，"
                "并保持 username=opencode（Basic Auth）。"
            )
        if response.status_code >= 500:
            raise AdapterUncertain(
                f"OpenCode 返回 {response.status_code}（请求结果不确定）：{response.text[:300]}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise AdapterRemoteError(
                f"OpenCode 返回 {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        return response

    # ---- health / session -------------------------------------------------

    async def health_check(self) -> HealthStatus:
        last_error: Exception | None = None
        for path in ("/api/health", "/global/health"):
            try:
                response = await self._request("GET", path, timeout=10)
            except AdapterRemoteError as exc:
                last_error = exc
                if exc.status_code == 404:
                    continue
                raise
            payload = response.json() if response.content else {}
            healthy = bool(payload.get("healthy", True))
            return HealthStatus(ok=healthy, detail=f"opencode @ {self._base}",
                                metadata={"path": path, "raw": payload})
        raise AdapterError(f"OpenCode 健康检查失败：{last_error}")

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        options = config or {}
        model = None
        provider_id = options.get("provider_id") or self.settings.provider_id
        model_id = options.get("model_id") or self.settings.model_id
        if provider_id and model_id:
            model = {"providerID": provider_id, "modelID": model_id}
        agent = options.get("agent") or self.settings.agent

        if self.settings.api_style == "v2":
            body: dict[str, Any] = {
                "location": {"directory": self.settings.directory or os.getcwd()},
            }
            if agent:
                body["agent"] = agent
            if model:
                body["model"] = {"id": model["modelID"], "providerID": model["providerID"]}
            response = await self._request("POST", "/api/session", json=body)
            payload = response.json()
            return payload["data"]["id"]

        body = {"title": f"orchestrator/{run_id}"}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = {"id": model["modelID"], "providerID": model["providerID"]}
        response = await self._request("POST", "/session", params=self._params(), json=body)
        return response.json()["id"]

    # ---- send / wait ------------------------------------------------------

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        if self.settings.api_style == "v2":
            return await self._send_v2(session_id, text)
        if self.settings.delivery == "sync":
            return await self._send_legacy_sync(session_id, text, timeout)
        return await self._send_legacy_async(session_id, text)

    def _legacy_payload(self, text: str, message_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if message_id:
            payload["messageID"] = message_id
        if self.settings.agent:
            payload["agent"] = self.settings.agent
        if self.settings.provider_id and self.settings.model_id:
            payload["model"] = {
                "providerID": self.settings.provider_id,
                "modelID": self.settings.model_id,
            }
        return payload

    async def _send_legacy_async(self, session_id: str, text: str) -> MessageRef:
        baseline = await self._legacy_assistant_ids(session_id)
        user_message_id = f"msg_{uuid4().hex}"
        await self._request(
            "POST", f"/session/{session_id}/prompt_async",
            params=self._params(), json=self._legacy_payload(text, user_message_id),
        )
        self._pending[user_message_id] = _Pending(
            session_id=session_id, message_id=user_message_id,
            baseline_assistant_ids=baseline, sent_at=time.time(), api_style="legacy",
        )
        return MessageRef(session_id=session_id, message_id=user_message_id)

    async def _send_legacy_sync(self, session_id: str, text: str,
                                timeout: float | None) -> MessageRef:
        response = await self._request(
            "POST", f"/session/{session_id}/message", params=self._params(),
            json=self._legacy_payload(text), timeout=timeout,
        )
        payload = response.json()
        info = payload.get("info") or {}
        reply = AgentReply(
            session_id=session_id,
            text=self._extract_legacy_parts(payload.get("parts") or []),
            provider_message_id=info.get("id"),
            stop_reason="end_turn",
            usage=info.get("tokens") or {},
            metadata={"mode": "sync", "raw_info": {
                key: info.get(key) for key in ("providerID", "modelID", "cost", "tokens", "error")
            }},
        )
        message_id = info.get("id") or f"msg_{uuid4().hex}"
        self._replies[message_id] = reply
        return MessageRef(session_id=session_id, message_id=message_id, metadata={"cached": True})

    async def _send_v2(self, session_id: str, text: str) -> MessageRef:
        baseline = await self._v2_assistant_ids(session_id)
        response = await self._request(
            "POST", f"/api/session/{session_id}/prompt",
            json={"prompt": {"text": text}, "delivery": "queue"},
        )
        payload = response.json().get("data") or {}
        message_id = payload.get("id") or f"msg_{uuid4().hex}"
        self._pending[message_id] = _Pending(
            session_id=session_id, message_id=message_id,
            baseline_assistant_ids=baseline, sent_at=time.time(), api_style="v2",
        )
        return MessageRef(
            session_id=session_id, message_id=message_id,
            metadata={"api_style": "v2", "baseline_assistant_ids": sorted(baseline)},
        )

    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        cached = self._replies.pop(ref.message_id, None)
        if cached is not None:
            return cached
        pending = self._pending.get(ref.message_id)
        if pending is None:
            raise AdapterError(f"未知的消息引用：{ref.message_id}")

        limit = timeout if timeout is not None else self._timeout
        deadline = asyncio.get_running_loop().time() + limit
        partial = ""
        while True:
            reply, partial = await self._poll(pending, deadline)
            if reply is not None:
                self._pending.pop(ref.message_id, None)
                return reply
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AdapterTimeout(
                    f"等待 OpenCode 响应超时（{limit}s）。已收到的内容：{partial[:300]}"
                )
            await asyncio.sleep(min(self.settings.poll_interval, remaining))

    async def _poll(self, pending: _Pending, deadline: float) -> tuple[AgentReply | None, str]:
        if pending.api_style == "legacy":
            return await self._poll_legacy(pending)
        return await self._poll_v2(pending, deadline)

    # ---- legacy polling ---------------------------------------------------

    async def _legacy_messages(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/session/{session_id}/message", params=self._params()
        )
        return response.json()

    async def _legacy_assistant_ids(self, session_id: str) -> set[str]:
        return {
            item["info"]["id"]
            for item in await self._legacy_messages(session_id)
            if (item.get("info") or {}).get("role") == "assistant"
        }

    async def _poll_legacy(self, pending: _Pending) -> tuple[AgentReply | None, str]:
        items = await self._legacy_messages(pending.session_id)
        chosen: dict[str, Any] | None = None
        chosen_score = -1
        chosen_created = -1.0
        for item in items:
            info = item.get("info") or {}
            if info.get("role") != "assistant":
                continue
            if info.get("id") in pending.baseline_assistant_ids:
                continue
            created = float((info.get("time") or {}).get("created") or 0)
            parent = info.get("parentID")
            if parent == pending.message_id:
                score = 2
            elif parent is None and created >= pending.sent_at * 1000 - 5000:
                score = 1
            else:
                continue
            if score > chosen_score or (score == chosen_score and created > chosen_created):
                chosen, chosen_score, chosen_created = item, score, created

        if chosen is None:
            return None, ""
        info = chosen.get("info") or {}
        text = self._extract_legacy_parts(chosen.get("parts") or [])
        if info.get("error"):
            raise AdapterRemoteError(f"OpenCode 返回错误：{info['error']}")
        completed = bool((info.get("time") or {}).get("completed"))
        if not completed:
            return None, text
        reply = AgentReply(
            session_id=pending.session_id,
            text=text,
            provider_message_id=info.get("id"),
            stop_reason="end_turn",
            usage=info.get("tokens") or {},
            metadata={"mode": "async", "raw_info": {
                key: info.get(key) for key in ("providerID", "modelID", "cost", "tokens", "finish")
            }},
        )
        return reply, text

    @staticmethod
    def _extract_legacy_parts(parts: list[dict[str, Any]]) -> str:
        texts = [
            part.get("text", "")
            for part in parts
            if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored")
        ]
        return "\n".join(text for text in texts if text).strip()

    # ---- v2 polling -------------------------------------------------------

    async def _v2_messages(self, session_id: str, *, order: str = "desc",
                           limit: int = 50) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/api/session/{session_id}/message",
            params={"order": order, "limit": limit},
        )
        return response.json().get("data") or []

    async def _v2_assistant_ids(self, session_id: str) -> set[str]:
        items = await self._v2_messages(session_id, order="asc", limit=200)
        return {item["id"] for item in items if item.get("type") == "assistant"}

    async def _poll_v2(self, pending: _Pending, deadline: float) -> tuple[AgentReply | None, str]:
        loop = asyncio.get_running_loop()
        remaining = max(0.0, deadline - loop.time())
        try:
            await self._request(
                "POST", f"/api/session/{pending.session_id}/wait",
                timeout=min(20.0, remaining) or 1.0,
            )
        except AdapterTimeout:
            pass
        except AdapterRemoteError as exc:
            if exc.status_code not in (404, 405):
                raise

        items = await self._v2_messages(pending.session_id)
        candidates = [
            item for item in items
            if item.get("type") == "assistant" and item.get("id") not in pending.baseline_assistant_ids
        ]
        if not candidates:
            return None, ""
        chosen = max(candidates, key=lambda item: float((item.get("time") or {}).get("created") or 0))
        text = "\n".join(
            part.get("text", "")
            for part in chosen.get("content") or []
            if part.get("type") == "text"
        ).strip()
        if chosen.get("error"):
            raise AdapterRemoteError(f"OpenCode 返回错误：{chosen['error']}")
        finished = bool((chosen.get("time") or {}).get("completed")) or bool(chosen.get("finish"))
        if not finished:
            return None, text
        reply = AgentReply(
            session_id=pending.session_id,
            text=text,
            provider_message_id=chosen.get("id"),
            stop_reason=chosen.get("finish") or "end_turn",
            usage=chosen.get("tokens") or {},
            metadata={"mode": "v2", "raw_info": {
                key: chosen.get(key) for key in ("agent", "model", "finish", "cost", "tokens")
            }},
        )
        return reply, text

    # ---- control ----------------------------------------------------------

    async def recover_reply(self, session_id: str, ref: MessageRef) -> AgentReply | None:
        """重启后查询 OpenCode 会话，确认消息是否已经产生完整回复。"""
        if self.settings.api_style == "v2" or ref.metadata.get("api_style") == "v2":
            baseline = set(ref.metadata.get("baseline_assistant_ids") or [])
            items = await self._v2_messages(session_id, order="desc", limit=100)
            candidates = [
                item for item in items
                if item.get("type") == "assistant" and item.get("id") not in baseline
            ]
            if not candidates:
                return None
            if len(candidates) != 1:
                # 无法唯一关联到本次请求：保留 UNKNOWN，交给人工核对
                return None
            chosen = candidates[0]
            finished = bool((chosen.get("time") or {}).get("completed")) or bool(chosen.get("finish"))
            if not finished:
                return None
            text = "\n".join(
                part.get("text", "") for part in chosen.get("content") or []
                if part.get("type") == "text"
            ).strip()
            return AgentReply(session_id=session_id, text=text,
                              provider_message_id=chosen.get("id"),
                              stop_reason=chosen.get("finish") or "end_turn",
                              usage=chosen.get("tokens") or {},
                              metadata={"mode": "recovered", "api_style": "v2"})

        items = await self._legacy_messages(session_id)
        for item in items:
            info = item.get("info") or {}
            if info.get("role") != "assistant" or info.get("parentID") != ref.message_id:
                continue
            if not (info.get("time") or {}).get("completed"):
                return None
            return AgentReply(
                session_id=session_id,
                text=self._extract_legacy_parts(item.get("parts") or []),
                provider_message_id=info.get("id"),
                stop_reason="end_turn",
                usage=info.get("tokens") or {},
                metadata={"mode": "recovered", "api_style": "legacy"},
            )
        return None

    async def cancel(self, session_id: str) -> None:
        path = (f"/api/session/{session_id}/interrupt"
                if self.settings.api_style == "v2"
                else f"/session/{session_id}/abort")
        await self._request("POST", path, params=self._params())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
