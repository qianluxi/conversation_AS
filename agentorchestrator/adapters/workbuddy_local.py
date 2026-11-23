from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..auth import WorkBuddyAuth
from ..config import WorkBuddyLocalSettings
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


class WorkBuddyLocalAdapter(AgentAdapter):
    """WorkBuddy 本地助理 API（OAuth2.1）适配器。

    注意：本地助理接口没有独立 session 概念，同一时间只允许一个进行中的对话。
    """

    kind = "workbuddy_local"
    session_placeholder = "workbuddy-local"

    def __init__(self, name: str, settings: WorkBuddyLocalSettings,
                 auth: WorkBuddyAuth | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
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
        self._inflight: str | None = None

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
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AdapterError(f"无法连接 WorkBuddy Open API（{self._base}）：{exc}") from exc
        except httpx.ReadTimeout as exc:
            raise AdapterTimeout("WorkBuddy 本地助理响应超时") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"请求 WorkBuddy Open API 失败：{exc}") from exc

        if response.status_code in (401, 403):
            if retry_auth:
                await self._auth.refresh()
                return await self._request(method, path, timeout=timeout,
                                           retry_auth=False, **kwargs)
            raise AdapterAuthError("WorkBuddy 鉴权失败，请重新执行 OAuth 授权。")
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

    async def health_check(self) -> HealthStatus:
        payload = await self._request("GET", "/openapi/v2/localassistant", timeout=15)
        online = bool((payload.get("data") or {}).get("online"))
        return HealthStatus(ok=online, detail="WorkBuddy PC 端本地助理在线" if online else "本地助理离线",
                            metadata={"request_id": payload.get("request_id")})

    async def create_session(self, run_id: str, config: dict[str, Any] | None = None) -> str:
        # 本地助理没有 session_id，使用固定值；协调器仍按 run 记录映射。
        return self.session_placeholder

    async def send_message(self, session_id: str, text: str, *,
                           timeout: float | None = None) -> MessageRef:
        if self._inflight is not None:
            raise AdapterError("WorkBuddy 本地助理同一时间只允许一个进行中的对话，请等待当前响应。")
        payload = await self._request(
            "POST", "/openapi/v2/localassistant/message",
            json={"content": text, "msg_type": "text"},
        )
        message_id = (payload.get("data") or {}).get("message_id")
        if not message_id:
            raise AdapterRemoteError("WorkBuddy 未返回 message_id", payload=payload)
        self._inflight = message_id
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
                    return AgentReply(
                        session_id=ref.session_id,
                        text=last_text,
                        provider_message_id=assistant[-1].get("message_id"),
                        stop_reason="end_turn",
                        metadata={"messages": assistant},
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise AdapterTimeout(
                        f"等待 WorkBuddy 本地助理响应超时（{limit}s）。已收到：{last_text[:300]}"
                    )
                await asyncio.sleep(min(self.settings.poll_interval, remaining))
        finally:
            self._inflight = None

    async def cancel(self, session_id: str) -> None:
        # 本地助理 Open API 没有取消接口，只能停止本地等待。
        log.warning("WorkBuddy 本地助理 API 不支持取消，已停止本地等待（服务端任务可能仍在执行）。")
        self._inflight = None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        await self._auth.aclose()
