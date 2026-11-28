from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
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


log = logging.getLogger(__name__)

PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
        self._request_timeout = settings.request_timeout
        self._cancel_timeout = settings.cancel_timeout
        self._inactivity_timeout = settings.inactivity_timeout
        self._client = client
        self._owns_client = client is None
        if self._client is None:
            auth = httpx.BasicAuth(settings.username, settings.password) if settings.password else None
            self._client = httpx.AsyncClient(
                auth=auth, verify=settings.verify_ssl,
                timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            )
        self._pending: dict[str, _Pending] = {}
        self._replies: dict[str, AgentReply] = {}
        self._permission_seen: set[str] = set()
        self.permission_handler: PermissionHandler | None = None

    # ---- http helpers -----------------------------------------------------

    def _params(self) -> dict[str, str]:
        return {"directory": self.settings.directory} if self.settings.directory else {}

    async def _request(self, method: str, path: str, *, timeout: float | None = None,
                       **kwargs: Any) -> httpx.Response:
        limit = httpx.Timeout(
            timeout if timeout is not None else self._request_timeout, connect=10.0,
        )
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

    async def get_path(self) -> dict[str, Any]:
        """返回 OpenCode 服务端的默认目录/工作区信息。"""
        response = await self._request("GET", "/path", timeout=10)
        return response.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/session/{session_id}", timeout=15)
        return response.json()

    async def list_models(self) -> dict[str, Any]:
        """返回已配置/可用的 provider/model 列表（绝不包含 API key）。"""
        response = await self._request("GET", "/config/providers", timeout=20)
        payload = response.json()
        providers = payload.get("providers") or []
        items: list[dict[str, Any]] = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            if not provider_id:
                continue
            models = provider.get("models") or {}
            if not isinstance(models, dict) or not models:
                continue
            configured = bool(provider.get("key"))
            if not configured:
                env_names = provider.get("env") or []
                configured = any(os.environ.get(str(name)) for name in env_names)
            for model_id, model in models.items():
                items.append({
                    "provider_id": provider_id,
                    "provider_name": provider.get("name") or provider_id,
                    "model_id": model_id,
                    "model_name": (model or {}).get("name") or model_id,
                    "configured": configured,
                })
        items.sort(key=lambda item: (not item["configured"], item["provider_id"], item["model_id"]))
        return {
            "models": items,
            "current": {
                "provider_id": self.settings.provider_id,
                "model_id": self.settings.model_id,
            },
        }

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
        return MessageRef(
            session_id=session_id, message_id=user_message_id,
            metadata={"api_style": "legacy", "sent_at": time.time()},
        )

    async def _send_legacy_sync(self, session_id: str, text: str,
                                timeout: float | None) -> MessageRef:
        response = await self._request(
            "POST", f"/session/{session_id}/message", params=self._params(),
            json=self._legacy_payload(text),
            timeout=timeout if timeout is not None else self._timeout,
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
            metadata={
                "api_style": "v2",
                "baseline_assistant_ids": sorted(baseline),
                "sent_at": time.time(),
            },
        )

    def _pending_from_ref(self, ref: MessageRef) -> _Pending:
        metadata = ref.metadata or {}
        style = metadata.get("api_style") or self.settings.api_style
        baseline = set(metadata.get("baseline_assistant_ids") or [])
        sent_at = float(metadata.get("sent_at") or time.time())
        return _Pending(
            session_id=ref.session_id, message_id=ref.message_id,
            baseline_assistant_ids=baseline, sent_at=sent_at, api_style=style,
        )

    # ---- permissions ------------------------------------------------------

    def _permission_roots(self) -> list[str]:
        roots = [self.settings.directory, tempfile.gettempdir(),
                 *self.settings.external_directory_allow]
        result: list[str] = []
        for root in roots:
            if not root:
                continue
            try:
                path = Path(os.path.expanduser(str(root)))
                if not path.is_absolute() and self.settings.directory:
                    path = Path(self.settings.directory) / path
                result.append(os.path.realpath(path))
            except OSError:
                continue
        return result

    def _pattern_allowed(self, pattern: str) -> bool:
        raw = str(pattern or "").strip().strip('"')
        if not raw:
            return False
        raw = raw.split("*", 1)[0].rstrip("/\\")
        if not raw:
            return False
        try:
            path = Path(os.path.expanduser(raw))
            if not path.is_absolute() and self.settings.directory:
                path = Path(self.settings.directory) / path
            resolved = Path(os.path.realpath(path))
        except OSError:
            return False
        for root in self._permission_roots():
            try:
                resolved.relative_to(Path(root))
                return True
            except ValueError:
                continue
        return False

    def _permission_decision(self, name: str, patterns: list[str]) -> str:
        name = name.strip().lower()
        allowed = bool(patterns) and all(self._pattern_allowed(item) for item in patterns)
        if name in {"read", "list", "glob", "grep"}:
            return "allow" if (not patterns or allowed) else "ask"
        if name in {"external_directory", "edit", "write"}:
            return "allow" if allowed else "ask"
        category = self.settings.permissions.category(
            f"{name} {' '.join(str(item) for item in patterns)}"
        )
        # 执行类命令交给 UI 决定；未知/网络/写入默认 ask，删除/安装仍拒绝。
        if category == "deny" and name in {"bash", "execute", "command", "shell"}:
            return "ask"
        return category

    async def _permission_requests(self, session_id: str) -> list[dict[str, Any]]:
        try:
            response = await self._request(
                "GET", "/permission", params=self._params(),
                timeout=min(self._request_timeout, 10.0),
            )
            items = response.json()
        except AdapterRemoteError as exc:
            if exc.status_code not in (404, 405):
                raise
            response = await self._request(
                "GET", "/api/permission/request",
                params=self._params(),
                timeout=min(self._request_timeout, 10.0),
            )
            items = response.json().get("data") or []
        return [
            item for item in items
            if isinstance(item, dict) and item.get("sessionID") == session_id
        ]

    async def _reply_permission(self, request_id: str, session_id: str,
                                reply: str) -> None:
        style = self.settings.api_style
        if style == "v2":
            path = f"/api/session/{session_id}/permission/{request_id}/reply"
            params = None
            body = {"reply": reply}
        else:
            path = f"/permission/{request_id}/reply"
            params = self._params()
            body = {"reply": reply}
        try:
            await self._request(
                "POST", path, params=params, json=body,
                timeout=min(self._request_timeout, 10.0),
            )
        except AdapterRemoteError as exc:
            if exc.status_code not in (404, 405):
                raise
            if style == "v2":
                await self._request(
                    "POST", f"/permission/{request_id}/reply",
                    params=self._params(), json=body,
                    timeout=min(self._request_timeout, 10.0),
                )
            else:
                await self._request(
                    "POST", f"/api/session/{session_id}/permission/{request_id}/reply",
                    json=body, timeout=min(self._request_timeout, 10.0),
                )

    def _permission_message(self, session_id: str, name: str,
                            patterns: list[str], metadata: Any) -> dict[str, Any]:
        detail = "; ".join(str(item) for item in patterns)[:300]
        return {
            "params": {
                "sessionId": session_id,
                "title": f"OpenCode 请求 {name}: {detail}" if detail else f"OpenCode 请求 {name}",
                "toolName": name,
                "toolCall": {
                    "name": name,
                    "title": name,
                    "kind": name,
                    "input": {"patterns": patterns, "metadata": metadata},
                },
                "options": [
                    {"optionId": "once", "name": "允许一次", "kind": "allow_once"},
                    {"optionId": "always", "name": "始终允许此路径", "kind": "allow_always"},
                    {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                ],
            },
        }

    @staticmethod
    def _permission_reply(result: dict[str, Any]) -> str:
        if str(result.get("reply") or "") in {"once", "always", "reject"}:
            return str(result["reply"])
        outcome = result.get("outcome") or {}
        if outcome.get("outcome") == "selected":
            option = str(outcome.get("optionId") or "")
            if option in {"once", "always", "reject"}:
                return option
            return "once"
        return "reject"

    async def _handle_permissions(self, session_id: str) -> None:
        try:
            requests = await self._permission_requests(session_id)
        except AdapterError as exc:
            log.debug("读取 OpenCode 权限请求失败：%s", exc)
            return
        for request in requests:
            request_id = str(request.get("id") or "")
            if not request_id or request_id in self._permission_seen:
                continue
            name = str(request.get("permission") or request.get("action") or "unknown")
            patterns = [str(item) for item in (
                request.get("patterns") or request.get("resources") or []
            )]
            decision = self._permission_decision(name, patterns)
            if decision == "allow":
                reply = "once"
            elif decision == "deny":
                reply = "reject"
            elif self.permission_handler is not None:
                self._permission_seen.add(request_id)
                try:
                    result = await self.permission_handler(
                        self._permission_message(
                            session_id, name, patterns, request.get("metadata"),
                        )
                    )
                    reply = self._permission_reply(result)
                except Exception as exc:  # noqa: BLE001 - 权限处理失败时拒绝，避免远端挂起
                    log.warning("OpenCode 权限请求处理失败，按拒绝处理：%s", exc)
                    reply = "reject"
            else:
                log.warning("OpenCode 请求 %s %s，但没有权限处理器，按拒绝处理",
                            name, patterns)
                reply = "reject"
            self._permission_seen.add(request_id)
            try:
                await self._reply_permission(request_id, session_id, reply)
            except AdapterError as exc:
                self._permission_seen.discard(request_id)
                log.warning("回复 OpenCode 权限请求失败：%s", exc)


    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        cached = self._replies.pop(ref.message_id, None)
        if cached is not None:
            return cached
        pending = self._pending.get(ref.message_id) or self._pending_from_ref(ref)
        self._pending.setdefault(ref.message_id, pending)

        limit = timeout if timeout is not None else self._timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        inactivity = self._inactivity_timeout
        partial = ""
        last_progress = None
        last_change = loop.time()
        while True:
            await self._handle_permissions(pending.session_id)
            reply, partial, running_tools = await self._poll(pending, deadline)
            if reply is not None:
                self._pending.pop(ref.message_id, None)
                return reply
            now = loop.time()
            if partial != last_progress:
                last_progress = partial
                last_change = now
            if running_tools > 0:
                # 活动工具可能长时间没有输出（install/build/test），不能按空闲处理。
                last_change = now
            if inactivity > 0 and running_tools == 0 and now - last_change >= inactivity:
                raise AdapterTimeout(
                    f"OpenCode 超过 {inactivity:.0f}s 没有新活动；已停止本地等待，"
                    f"远端可能仍在执行。可使用“继续等待”、核对远端或请求中止。"
                    f"最近进展：{partial[-800:]}"
                )
            remaining = deadline - now
            if remaining <= 0:
                raise AdapterTimeout(
                    f"等待 OpenCode 响应达到上限（{limit}s）；已停止本地等待，"
                    f"远端可能仍在执行。可使用“继续等待”、核对远端或请求中止。"
                    f"最近进展：{partial[-800:]}"
                )
            await asyncio.sleep(min(self.settings.poll_interval, remaining))

    async def _poll(
        self, pending: _Pending, deadline: float,
    ) -> tuple[AgentReply | None, str, int]:
        if pending.api_style == "legacy":
            return await self._poll_legacy(pending, deadline)
        return await self._poll_v2(pending, deadline)

    # ---- legacy polling ---------------------------------------------------

    async def _legacy_messages(
        self, session_id: str, *, timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/session/{session_id}/message", params=self._params(), timeout=timeout,
        )
        return response.json()

    async def _legacy_assistant_ids(self, session_id: str) -> set[str]:
        return {
            item["info"]["id"]
            for item in await self._legacy_messages(session_id)
            if (item.get("info") or {}).get("role") == "assistant"
        }

    async def _poll_legacy(
        self, pending: _Pending, deadline: float,
    ) -> tuple[AgentReply | None, str, int]:
        loop = asyncio.get_running_loop()
        remaining = max(0.1, deadline - loop.time())
        request_limit = min(self._request_timeout, remaining)
        items = await self._legacy_messages(pending.session_id, timeout=request_limit)
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
            return None, "等待 OpenCode 开始响应…", 0
        info = chosen.get("info") or {}
        text = self._extract_legacy_parts(chosen.get("parts") or [])
        progress = self._legacy_progress(chosen.get("parts") or [], text)
        running = self._running_tool_count(chosen.get("parts") or [])
        if info.get("error"):
            raise AdapterRemoteError(f"OpenCode 返回错误：{info['error']}")
        completed = bool((info.get("time") or {}).get("completed"))
        if not completed:
            return None, progress, running
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
        return reply, text, running

    @staticmethod
    def _extract_legacy_parts(parts: list[dict[str, Any]]) -> str:
        texts = [
            part.get("text", "")
            for part in parts
            if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored")
        ]
        return "\n".join(text for text in texts if text).strip()

    @staticmethod
    def _running_tool_count(parts: list[dict[str, Any]]) -> int:
        count = 0
        for part in parts:
            if part.get("type") != "tool":
                continue
            status = str((part.get("state") or {}).get("status") or "").lower()
            if status in {"running", "pending", "in_progress", "started"}:
                count += 1
        return count

    @staticmethod
    def _legacy_progress(parts: list[dict[str, Any]], text: str) -> str:
        lines: list[str] = []
        if text:
            lines.append(text)
        reasoning = sum(
            len(part.get("text") or "") for part in parts if part.get("type") == "reasoning"
        )
        if reasoning:
            lines.append(f"[思考中：{reasoning} 字]")
        for part in parts:
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            status = state.get("status") or "unknown"
            tool = part.get("tool") or "tool"
            detail = ""
            payload = state.get("input") or {}
            if isinstance(payload, dict):
                detail = (
                    payload.get("command")
                    or payload.get("filePath")
                    or payload.get("pattern")
                    or payload.get("description")
                    or ""
                )
            if detail:
                detail = " ".join(str(detail).split())[:160]
                lines.append(f"[工具 {tool} {status}] {detail}")
            else:
                lines.append(f"[工具 {tool} {status}]")
        return "\n".join(lines).strip()

    # ---- v2 polling -------------------------------------------------------

    async def _v2_messages(self, session_id: str, *, order: str = "desc",
                           limit: int = 50, timeout: float | None = None) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/api/session/{session_id}/message",
            params={"order": order, "limit": limit}, timeout=timeout,
        )
        return response.json().get("data") or []

    async def _v2_assistant_ids(self, session_id: str) -> set[str]:
        items = await self._v2_messages(session_id, order="asc", limit=200)
        return {item["id"] for item in items if item.get("type") == "assistant"}

    async def _poll_v2(
        self, pending: _Pending, deadline: float,
    ) -> tuple[AgentReply | None, str, int]:
        loop = asyncio.get_running_loop()
        remaining = max(0.1, deadline - loop.time())
        request_limit = min(self._request_timeout, remaining)
        wait_paths = (
            f"/api/experimental/session/{pending.session_id}/wait",
            f"/api/session/{pending.session_id}/wait",
        )
        for path in wait_paths:
            try:
                await self._request("POST", path, timeout=min(request_limit, 20.0))
                break
            except AdapterTimeout:
                break
            except AdapterRemoteError as exc:
                if exc.status_code not in (404, 405):
                    raise

        remaining = max(0.1, deadline - loop.time())
        items = await self._v2_messages(
            pending.session_id, timeout=min(self._request_timeout, remaining),
        )
        candidates = [
            item for item in items
            if item.get("type") == "assistant" and item.get("id") not in pending.baseline_assistant_ids
        ]
        if not candidates:
            return None, "等待 OpenCode 开始响应…", 0
        chosen = max(candidates, key=lambda item: float((item.get("time") or {}).get("created") or 0))
        content = chosen.get("content") or []
        text = "\n".join(
            part.get("text", "")
            for part in content
            if part.get("type") == "text"
        ).strip()
        progress = self._legacy_progress(content, text)
        running = self._running_tool_count(content)
        if chosen.get("error"):
            raise AdapterRemoteError(f"OpenCode 返回错误：{chosen['error']}")
        finished = bool((chosen.get("time") or {}).get("completed")) or bool(chosen.get("finish"))
        if not finished:
            return None, progress, running
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
        return reply, text, running

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

    async def cancel(self, session_id: str, *, timeout: float | None = None) -> None:
        path = (f"/api/session/{session_id}/interrupt"
                if self.settings.api_style == "v2"
                else f"/session/{session_id}/abort")
        await self._request(
            "POST", path, params=self._params(),
            timeout=timeout if timeout is not None else self._cancel_timeout,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
