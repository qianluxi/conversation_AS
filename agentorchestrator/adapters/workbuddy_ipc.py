from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..config import WorkBuddyIpcSettings
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
)

log = logging.getLogger(__name__)

ChatFn = Callable[[list[dict[str, str]]], str]

_LIBRARY_SUFFIX = Path(
    "resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library"
)


def _candidate_library_dirs(override: str | None) -> list[Path]:
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    env = os.environ.get("WORKBUDDY_IPC_LIB")
    if env:
        candidates.append(Path(env))
    for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "WorkBuddy" / _LIBRARY_SUFFIX)
    for drive in "CDEFGH":
        candidates.append(Path(f"{drive}:/Program Files/WorkBuddy") / _LIBRARY_SUFFIX)
        candidates.append(Path(f"{drive}:/Program Files (x86)/WorkBuddy") / _LIBRARY_SUFFIX)
    return candidates


class WorkBuddyIpcAdapter(AgentAdapter):
    """实验性 WorkBuddy 适配器：走桌面端本地 wbipc，避开开放平台 OAuth。

    依赖 WorkBuddy 正在运行且已登录，并使用其自带的 `workbuddy_ipc` Python
    客户端（随内置 skills 一起安装）。调用 `POST /v2/chat/completions`
    （SSE），属于 WorkBuddy 内部接口，不保证跨版本兼容。
    """

    kind = "workbuddy_ipc"

    def __init__(self, name: str, settings: WorkBuddyIpcSettings,
                 chat_fn: ChatFn | None = None) -> None:
        super().__init__(name)
        self.settings = settings
        self._chat_fn = chat_fn
        self._module: Any = None
        self._client: Any = None
        self._pipe: Any = None
        self._connect_lock = threading.Lock()
        self._send_lock = asyncio.Lock()
        self._history: dict[str, list[dict[str, str]]] = {}
        self._replies: dict[str, AgentReply] = {}
        self._last_meta: dict[str, Any] = {}

    # ---- vendor bootstrap -------------------------------------------------

    def _import_module(self) -> Any:
        if self._module is not None:
            return self._module
        errors: list[str] = []
        for root in _candidate_library_dirs(self.settings.library_path):
            init_file = root / "workbuddy_ipc" / "__init__.py"
            if not init_file.exists():
                continue
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            try:
                self._module = importlib.import_module("workbuddy_ipc")
                log.info("已加载 WorkBuddy IPC 客户端：%s", root)
                return self._module
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{root}: {exc}")
        detail = "; ".join(errors) if errors else "未找到 workbuddy_ipc/__init__.py"
        raise AdapterError(
            "无法加载 WorkBuddy IPC 客户端。请确认 WorkBuddy 已安装，"
            "或通过配置 library_path / 环境变量 WORKBUDDY_IPC_LIB 指向 "
            "`...\\WorkBuddy\\resources\\app.asar.unpacked\\resources\\plugins\\"
            "workbuddy-builtin\\skills\\library`。详情：" + detail
        )

    def _map_wbipc_error(self, exc: Exception) -> AdapterError:
        code = getattr(exc, "code", "")
        message = getattr(exc, "message", str(exc))
        if code == "E_NOT_WORKBUDDY":
            return AdapterError(
                "WorkBuddy 桌面端未运行，或 wbipc endpoint 不存在。请先启动并登录 WorkBuddy。"
            )
        if code == "E_CONSENT_REQUIRED":
            return AdapterAuthError(
                "WorkBuddy 要求授权本地 IPC 客户端。请在 WorkBuddy 桌面端确认/允许后重试。"
            )
        if code == "E_ENDPOINT_UNTRUSTED":
            return AdapterError(
                "WorkBuddy wbipc endpoint 无法证明身份（可能被占用），已拒绝连接。"
            )
        if code in ("E_TIMEOUT", "E_UPSTREAM", "E_INTERNAL"):
            return AdapterUncertain(f"WorkBuddy IPC 调用结果不确定（{code}）：{message}")
        if code in ("E_POLICY_DENIED", "E_REVOKED"):
            return AdapterAuthError(f"WorkBuddy IPC 被拒绝（{code}）：{message}")
        return AdapterError(f"WorkBuddy IPC 错误（{code or 'unknown'}）：{message}")

    def _ensure_pipe(self) -> Any:
        with self._connect_lock:
            if self._pipe is not None:
                return self._pipe
            module = self._import_module()
            try:
                client = module.connect(
                    client_id=self.settings.client_id,
                    client_kind=self.settings.client_kind,
                )
            except Exception as exc:  # noqa: BLE001 - 映射 WorkBuddy 自定义错误
                wbipc_error = getattr(module, "WbipcError", None)
                if wbipc_error is not None and isinstance(exc, wbipc_error):
                    raise self._map_wbipc_error(exc) from exc
                raise AdapterError(f"连接 WorkBuddy IPC 失败：{exc}") from exc
            try:
                pipe = client.get_pipe("wb.request")
                if "http.fetch" not in (getattr(pipe, "methods", None) or []):
                    raise AdapterError(
                        "WorkBuddy IPC 的 wb.request 不提供 http.fetch，客户端版本可能不兼容。"
                    )
            except BaseException:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._client = client
            self._pipe = pipe
            return pipe

    def _invoke_sync(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        pipe = self._ensure_pipe()
        module = self._module
        try:
            return pipe.invoke("http.fetch", request, timeout=float(timeout) + 5.0)
        except Exception as exc:  # noqa: BLE001
            wbipc_error = getattr(module, "WbipcError", None)
            if wbipc_error is not None and isinstance(exc, wbipc_error):
                raise self._map_wbipc_error(exc) from exc
            raise AdapterError(f"WorkBuddy IPC 请求失败：{exc}") from exc

    async def _invoke(self, request: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        limit = timeout if timeout is not None else self.settings.timeout
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._invoke_sync, request, limit),
                timeout=limit + 20.0,
            )
        except asyncio.TimeoutError as exc:
            raise AdapterTimeout(f"WorkBuddy IPC 调用超时（{limit}s）") from exc

    # ---- health / session -------------------------------------------------

    async def health_check(self) -> HealthStatus:
        if self._chat_fn is not None:
            return HealthStatus(True, "workbuddy_ipc（测试 chat_fn）")
        try:
            client = await asyncio.to_thread(lambda: (self._ensure_pipe(), self._client)[1])
            pipes = getattr(client, "pipes", []) or []
            return HealthStatus(
                True, f"WorkBuddy IPC 可用：{', '.join(pipes)}",
                metadata={"client_id": self.settings.client_id},
            )
        except AdapterError as exc:
            return HealthStatus(False, str(exc))

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        # 无远端 session；用 run_id 隔离本地历史。
        return f"wbipc:{run_id}"

    # ---- chat -------------------------------------------------------------

    @staticmethod
    def parse_sse(text: str) -> tuple[str, str, dict[str, Any] | None]:
        content: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning.append(str(delta["reasoning_content"]))
        return "".join(content), "".join(reasoning), usage

    async def _chat(self, messages: list[dict[str, str]],
                    timeout: float | None) -> tuple[str, str, dict[str, Any]]:
        if self._chat_fn is not None:
            text = await asyncio.to_thread(self._chat_fn, messages)
            return text, "", {"mode": "test"}

        body = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = {
            "method": "POST",
            "path": self.settings.chat_path,
            "headers": {"content-type": "application/json", "accept": "text/event-stream"},
            "body_b64": base64.b64encode(raw).decode("ascii"),
        }
        result = await self._invoke(request, timeout)
        status = int(result.get("status") or 0)
        body_bytes = result.get("body_b64") or b""
        try:
            text = base64.b64decode(body_bytes).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""

        if status in (401, 403):
            raise AdapterAuthError(
                f"WorkBuddy chat 接口鉴权失败（{status}）：请确认桌面端已登录。{text[:200]}"
            )
        if status >= 500:
            raise AdapterUncertain(
                f"WorkBuddy chat 接口返回 {status}（结果不确定）：{text[:300]}"
            )
        if status >= 400:
            raise AdapterRemoteError(
                f"WorkBuddy chat 接口返回 {status}: {text[:500]}", status_code=status,
            )

        content, reasoning, usage = self.parse_sse(text)
        metadata: dict[str, Any] = {"usage": usage or {}}
        if not content.strip() and reasoning.strip():
            content = reasoning
            metadata["fallback_reasoning"] = True
        if not content.strip():
            raise AdapterRemoteError("WorkBuddy chat 返回空回复", payload=text[:500])
        return content, reasoning, metadata

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        async with self._send_lock:
            history = self._history.setdefault(session_id, [])
            messages = [*history, {"role": "user", "content": text}]
            content, reasoning, metadata = await self._chat(messages, timeout)
            history.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": content},
            ])
            limit = max(0, int(self.settings.history_limit)) * 2
            if limit and len(history) > limit:
                del history[:-limit]
            message_id = f"wbipc-{uuid4().hex}"
            self._replies[message_id] = AgentReply(
                session_id=session_id, text=content,
                provider_message_id=message_id, stop_reason="end_turn",
                usage=(metadata.get("usage") or {}),
                metadata={**metadata, "model": self.settings.model,
                          "reasoning_chars": len(reasoning)},
            )
            return MessageRef(session_id=session_id, message_id=message_id,
                              metadata={"cache": True})

    async def wait_response(self, ref: MessageRef, *, timeout: float | None = None) -> AgentReply:
        reply = self._replies.pop(ref.message_id, None)
        if reply is None:
            raise AdapterError(f"未知的 WorkBuddy IPC 消息引用：{ref.message_id}")
        return reply

    async def recover_reply(self, session_id: str, ref: MessageRef) -> AgentReply | None:
        # chat 接口是无状态调用，无法在重启后关联；保持 UNKNOWN。
        return None

    async def cancel_wait(self, session_id: str) -> None:
        log.warning("WorkBuddy IPC chat 为同步调用，无法中止正在进行的请求。")

    async def remote_abort(self, session_id: str) -> None:
        raise AdapterError("WorkBuddy IPC chat 接口不提供远端取消。")

    async def cancel(self, session_id: str) -> None:
        await self.cancel_wait(session_id)

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        self._pipe = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
