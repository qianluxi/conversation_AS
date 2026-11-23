from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from ..auth import WorkBuddyAuth
from ..config import WorkBuddyAcpSettings
from ..sse import iter_sse
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRemoteError,
    AdapterTimeout,
    AgentAdapter,
    AgentReply,
    HealthStatus,
    MessageRef,
)

log = logging.getLogger(__name__)

PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class _Task:
    task_id: str
    link: str
    token: str
    expire_at: int | None = None


@dataclass
class _AcpSession:
    session_id: str
    task: _Task | None = None
    ready: bool = False


@dataclass
class _PromptState:
    session_id: str
    task_id: str
    rpc_id: int
    future: asyncio.Future[dict[str, Any]]
    parts: list[str] = field(default_factory=list)


class WorkBuddyAcpAdapter(AgentAdapter):
    """WorkBuddy 云端任务 ACP 适配器（SSE + JSON-RPC 2.0）。

    建连顺序：创建任务 -> GET {link} 建立 SSE -> initialize -> session/load
    -> session/prompt。流式内容经 session/update 通知推回。
    """

    kind = "workbuddy_acp"

    def __init__(self, name: str, settings: WorkBuddyAcpSettings,
                 auth: WorkBuddyAuth | None = None,
                 control_client: httpx.AsyncClient | None = None,
                 acp_client: httpx.AsyncClient | None = None) -> None:
        super().__init__(name)
        self.settings = settings
        self._base = settings.base_url.rstrip("/")
        self._auth = auth or WorkBuddyAuth(
            base_url=settings.base_url, client_id=settings.client_id,
            client_secret=settings.client_secret, redirect_uri=settings.redirect_uri,
            token_file=settings.token_file, scope=settings.scope,
        )
        self._control = control_client
        self._owns_control = control_client is None
        self._acp = acp_client or httpx.AsyncClient(
            verify=settings.verify_ssl,
            timeout=httpx.Timeout(connect=15.0, read=None, write=30.0, pool=None),
        )
        self._owns_acp = acp_client is None
        self._stream: httpx.Response | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connection_id: str | None = None
        self._stream_error: Exception | None = None
        self._next_rpc_id = 1
        self._pending_rpc: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._prompts: dict[int, _PromptState] = {}
        self._active_by_session: dict[str, _PromptState] = {}
        self._sessions: dict[str, _AcpSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.permission_handler: PermissionHandler | None = None

    # ---- control plane ----------------------------------------------------

    def _control_http(self) -> httpx.AsyncClient:
        if self._control is None:
            self._control = httpx.AsyncClient(
                verify=self.settings.verify_ssl,
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._control

    async def _control_request(self, method: str, path: str, *, retry_auth: bool = True,
                               json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._auth.get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            response = await self._control_http().request(
                method, f"{self._base}{path}", headers=headers, json=json_body,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AdapterError(f"无法连接 WorkBuddy 控制面（{self._base}）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"请求 WorkBuddy 控制面失败：{exc}") from exc

        if response.status_code in (401, 403):
            if retry_auth:
                await self._auth.refresh()
                return await self._control_request(method, path, retry_auth=False, json_body=json_body)
            raise AdapterAuthError("WorkBuddy 鉴权失败，请重新执行 OAuth 授权。")
        if response.status_code >= 400:
            raise AdapterRemoteError(
                f"WorkBuddy 控制面返回 {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        payload = response.json()
        if isinstance(payload, dict) and int(payload.get("code", 0) or 0) != 0:
            raise AdapterRemoteError(
                f"WorkBuddy 业务错误 {payload.get('code')}: {payload.get('msg')}", payload=payload
            )
        return payload

    async def _create_task(self, prompt: str, name: str) -> _Task:
        payload = await self._control_request(
            "POST", "/openapi/v2/tasks", json_body={"prompt": prompt, "name": name},
        )
        data = payload.get("data", payload)
        task_id = data.get("task_id")
        if not task_id:
            raise AdapterRemoteError("WorkBuddy 未返回 task_id", payload=payload)
        task = _Task(task_id=task_id, link=data.get("link") or "",
                     token=data.get("token") or "", expire_at=data.get("expire_at"))
        if task.link and task.token:
            return task
        return await self._wait_task_ready(task_id, deadline_seconds=90)

    async def _wait_task_ready(self, task_id: str, *, deadline_seconds: float) -> _Task:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_seconds
        while True:
            payload = await self._control_request("GET", f"/openapi/v2/tasks/{task_id}")
            data = payload.get("data", payload)
            if data.get("link") and data.get("token"):
                return _Task(task_id=task_id, link=data["link"], token=data["token"],
                             expire_at=data.get("expire_at"))
            if loop.time() >= deadline:
                raise AdapterTimeout("等待 WorkBuddy 任务 ACP link/token 超时")
            await asyncio.sleep(self.settings.poll_interval)

    async def _refresh_task_token(self, task: _Task) -> None:
        payload = await self._control_request("GET", f"/openapi/v2/tasks/{task.task_id}")
        data = payload.get("data", payload)
        if data.get("token"):
            task.token = data["token"]
        if data.get("link"):
            task.link = data["link"]

    # ---- health / session -------------------------------------------------

    async def health_check(self) -> HealthStatus:
        payload = await self._control_request("GET", "/openapi/v2/tasks?page=1&size=1")
        total = payload.get("total", len(payload.get("tasks") or []))
        return HealthStatus(ok=True, detail="WorkBuddy 控制面可用",
                            metadata={"tasks_total": total})

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        prompt = (config or {}).get("prompt") or self.settings.bootstrap_prompt
        task = await self._create_task(prompt, name=run_id)
        session_id = f"acp-{uuid4().hex[:8]}"
        self._sessions[session_id] = _AcpSession(session_id=session_id, task=task)
        self._locks[session_id] = asyncio.Lock()
        return session_id

    async def _ensure_ready(self, session: _AcpSession, timeout: float) -> None:
        if session.ready:
            return
        task = session.task
        if task is None:
            raise AdapterError("ACP 会话缺少 task 信息")
        if self._stream is not None:
            raise AdapterError("该适配器实例已绑定一个 ACP 连接，请为每个会话创建独立适配器。")

        headers = {"Authorization": f"Bearer {task.token}", "Accept": "text/event-stream"}
        request = self._acp.build_request("GET", task.link, headers=headers)
        response = await self._acp.send(request, stream=True)
        if response.status_code in (401, 403):
            await response.aclose()
            await self._refresh_task_token(task)
            headers["Authorization"] = f"Bearer {task.token}"
            request = self._acp.build_request("GET", task.link, headers=headers)
            response = await self._acp.send(request, stream=True)
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")[:300]
            await response.aclose()
            raise AdapterRemoteError(
                f"建立 WorkBuddy ACP SSE 连接失败：{response.status_code} {body}",
                status_code=response.status_code,
            )

        self._connection_id = response.headers.get("acp-connection-id")
        if not self._connection_id:
            await response.aclose()
            raise AdapterRemoteError("WorkBuddy ACP 响应缺少 Acp-Connection-Id 头")
        self._stream = response
        self._reader_task = asyncio.create_task(self._read_stream(response))

        await self._rpc(
            "initialize",
            {"protocolVersion": 1,
             "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}}},
            timeout=timeout, task=task,
        )
        await self._rpc(
            "session/load",
            {"sessionId": task.task_id, "cwd": self.settings.cwd, "mcpServers": []},
            timeout=timeout, task=task,
        )
        session.ready = True

    # ---- ACP transport ----------------------------------------------------

    def _acp_headers(self, task: _Task) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {task.token}", "Content-Type": "application/json"}
        if self._connection_id:
            headers["Acp-Connection-Id"] = self._connection_id
        return headers

    async def _post_acp(self, payload: dict[str, Any], *, task: _Task,
                        retry_auth: bool = True) -> None:
        try:
            response = await self._acp.post(
                task.link, json=payload, headers=self._acp_headers(task), timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"向 WorkBuddy ACP 通道发送请求失败：{exc}") from exc
        if response.status_code in (401, 403):
            if retry_auth:
                await self._refresh_task_token(task)
                return await self._post_acp(payload, task=task, retry_auth=False)
            raise AdapterAuthError("WorkBuddy ACP 鉴权失败，token 已失效。")
        if response.status_code >= 400:
            raise AdapterRemoteError(
                f"WorkBuddy ACP 返回 {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )

    def _next_id(self) -> int:
        current = self._next_rpc_id
        self._next_rpc_id += 1
        return current

    async def _rpc(self, method: str, params: dict[str, Any], *, timeout: float,
                   task: _Task | None = None) -> dict[str, Any]:
        rpc_id = self._next_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_rpc[rpc_id] = future
        await self._post_acp(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
            task=task or self._current_task(),
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AdapterTimeout(f"ACP {method} 等待响应超时（{timeout}s）") from exc
        finally:
            self._pending_rpc.pop(rpc_id, None)

    def _current_task(self) -> _Task:
        for session in self._sessions.values():
            if session.ready and session.task is not None:
                return session.task
        for session in self._sessions.values():
            if session.task is not None:
                return session.task
        raise AdapterError("尚未创建 ACP 任务")

    async def _read_stream(self, response: httpx.Response) -> None:
        try:
            async for _event, data in iter_sse(response.aiter_lines()):
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 记录并传递给等待方
            self._stream_error = exc
            log.warning("WorkBuddy ACP SSE 读取中断：%s", exc)
            self._fail_all(AdapterError(f"ACP SSE 连接中断：{exc}"))
        finally:
            await response.aclose()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._handle_server_request(message)
            else:
                self._handle_notification(message)
            return
        rpc_id = message.get("id")
        if rpc_id is None:
            return
        state = self._prompts.get(int(rpc_id)) if isinstance(rpc_id, int) else None
        future = self._pending_rpc.get(int(rpc_id)) if isinstance(rpc_id, int) else None
        error = message.get("error")
        result = message.get("result") or {}
        if error:
            exc = AdapterRemoteError(f"ACP 错误：{error}")
            if future and not future.done():
                future.set_exception(exc)
            if state and not state.future.done():
                state.future.set_exception(exc)
            return
        if future and not future.done():
            future.set_result(result)
        if state and not state.future.done():
            state.future.set_result(result)

    def _handle_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") != "session/update":
            return
        params = message.get("params") or {}
        update = params.get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        chunks: list[str] = []
        if isinstance(content, dict) and content.get("type") == "text":
            chunks.append(content.get("text") or "")
        elif isinstance(content, list):
            chunks.extend(
                item.get("text") or "" for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        state = self._active_by_session.get(params.get("sessionId") or "")
        if state is not None:
            state.parts.extend(chunk for chunk in chunks if chunk)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        try:
            if method == "session/request_permission":
                if self.permission_handler is not None:
                    result = await self.permission_handler(message)
                elif self.settings.auto_approve:
                    options = params.get("options") or []
                    allowed = [opt for opt in options if opt.get("kind") in ("allow_once", "allow_always")]
                    picked = (allowed or options or [{}])[0]
                    result = {"outcome": {"outcome": "selected", "optionId": picked.get("optionId")}}
                else:
                    result = {"outcome": {"outcome": "cancelled"}}
            else:
                await self._post_acp(
                    {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": f"Unsupported method: {method}"}},
                    task=self._current_task(),
                )
                return
            await self._post_acp(
                {"jsonrpc": "2.0", "id": message["id"], "result": result},
                task=self._current_task(),
            )
        except Exception as exc:  # noqa: BLE001 - 不让 UI 请求拖垮读流
            log.warning("处理 ACP 服务端请求失败：%s", exc)

    def _fail_all(self, error: Exception) -> None:
        for future in list(self._pending_rpc.values()):
            if not future.done():
                future.set_exception(error)
        for state in list(self._prompts.values()):
            if not state.future.done():
                state.future.set_exception(error)

    # ---- send / wait ------------------------------------------------------

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        session = self._sessions.get(session_id)
        if session is None:
            raise AdapterError(f"未知的 ACP 会话：{session_id}")
        limit = timeout if timeout is not None else self.settings.timeout
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._ensure_ready(session, limit)
            if self._stream_error is not None:
                raise AdapterError(f"ACP 连接不可用：{self._stream_error}")
            task = session.task
            assert task is not None
            rpc_id = self._next_id()
            state = _PromptState(
                session_id=session_id, task_id=task.task_id, rpc_id=rpc_id,
                future=asyncio.get_running_loop().create_future(),
            )
            self._prompts[rpc_id] = state
            self._active_by_session[task.task_id] = state
            await self._post_acp(
                {"jsonrpc": "2.0", "id": rpc_id, "method": "session/prompt",
                 "params": {"sessionId": task.task_id,
                            "prompt": [{"type": "text", "text": text}]}},
                task=task,
            )
            return MessageRef(session_id=session_id, message_id=f"acp-{rpc_id}",
                              metadata={"rpc_id": rpc_id, "task_id": task.task_id})

    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        rpc_id = ref.metadata.get("rpc_id")
        state = self._prompts.get(int(rpc_id)) if rpc_id is not None else None
        if state is None:
            raise AdapterError(f"未知的 ACP 消息引用：{ref.message_id}")
        limit = timeout if timeout is not None else self.settings.timeout
        try:
            result = await asyncio.wait_for(asyncio.shield(state.future), timeout=limit)
        except asyncio.TimeoutError as exc:
            raise AdapterTimeout(f"等待 WorkBuddy ACP 响应超时（{limit}s）") from exc
        self._active_by_session.pop(state.task_id, None)
        return AgentReply(
            session_id=state.session_id,
            text="".join(state.parts).strip(),
            provider_message_id=ref.message_id,
            stop_reason=result.get("stopReason"),
            usage=result.get("usage") or {},
            metadata={"rpc_id": state.rpc_id, "task_id": state.task_id},
        )

    async def cancel(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.task is None:
            return
        try:
            await self._post_acp(
                {"jsonrpc": "2.0", "method": "session/cancel",
                 "params": {"sessionId": session.task.task_id}},
                task=session.task,
            )
        except AdapterError as exc:
            log.warning("发送 ACP cancel 失败：%s", exc)

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        self._fail_all(AdapterError("适配器已关闭"))
        if self._owns_acp:
            await self._acp.aclose()
        if self._owns_control and self._control is not None:
            await self._control.aclose()
        await self._auth.aclose()
