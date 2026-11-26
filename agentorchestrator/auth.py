from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .adapters.base import AdapterAuthError, AdapterError


class WorkBuddyAuth:
    """WorkBuddy OAuth 2.1 授权码 + refresh_token 管理。"""

    def __init__(self, *, base_url: str = "https://www.workbuddy.cn",
                 client_id: str = "", client_secret: str = "",
                 redirect_uri: str = "", token_file: str = "data/workbuddy_token.json",
                 scope: str = "", timeout: float = 30.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_file = Path(token_file)
        self.scope = scope
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=10.0))
        return self._client

    # ---- local token store ------------------------------------------------

    def load_token(self) -> dict[str, Any]:
        if not self.token_file.exists():
            return {}
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_token(self, payload: dict[str, Any]) -> None:
        data = dict(payload)
        expires_in = int(data.pop("expires_in", 0) or 0)
        if expires_in:
            data["expires_at"] = time.time() + expires_in - 60
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def clear_token(self) -> None:
        if self.token_file.exists():
            self.token_file.unlink()

    def status(self) -> dict[str, Any]:
        token = self.load_token()
        expires_at = token.get("expires_at")
        return {
            "token_file": str(self.token_file),
            "has_access_token": bool(token.get("access_token")),
            "has_refresh_token": bool(token.get("refresh_token")),
            "expires_at": expires_at,
            "expired": bool(expires_at) and float(expires_at) <= time.time(),
            "scope": token.get("scope"),
            "open_id": token.get("open_id"),
        }

    # ---- oauth flow -------------------------------------------------------

    STATE_TTL_SECONDS = 600

    @property
    def state_file(self) -> Path:
        return self.token_file.parent / "workbuddy_oauth_state.json"

    def _save_state(self, state: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "state": state,
            "created_at": time.time(),
            "redirect_uri": self.redirect_uri,
        }, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    def _consume_state(self, state: str | None) -> str:
        if not self.state_file.exists():
            raise AdapterAuthError(
                "没有待验证的 OAuth state。请先运行 `auth url` 生成授权链接，"
                "再带上回调中的 code 和 state 执行 `auth exchange`。"
            )
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterAuthError("本地 OAuth state 文件损坏，请重新运行 `auth url`。") from exc
        created_at = float(saved.get("created_at") or 0)
        if time.time() - created_at > self.STATE_TTL_SECONDS:
            try:
                self.state_file.unlink()
            except OSError:
                pass
            raise AdapterAuthError("OAuth state 已过期（超过 10 分钟），请重新运行 `auth url`。")
        expected = str(saved.get("state") or "")
        if not state or not secrets.compare_digest(state, expected):
            raise AdapterAuthError("OAuth state 不匹配，已拒绝交换 code（可能是 CSRF 或复制错误）。")
        try:
            self.state_file.unlink()
        except OSError:
            pass
        return state

    def begin_authorization(self, state: str | None = None) -> tuple[str, str]:
        """生成授权链接并持久化 state，供 exchange_code 校验。"""
        chosen = state or secrets.token_urlsafe(24)
        url = self.build_authorize_url(state=chosen)
        self._save_state(chosen)
        return url, chosen

    def build_authorize_url(self, state: str | None = None) -> str:
        if not self.client_id or not self.redirect_uri:
            raise AdapterAuthError("缺少 client_id 或 redirect_uri，无法生成授权地址。")
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        if self.scope:
            query["scope"] = self.scope
        if state:
            query["state"] = state
        return f"{self.base_url}/openapi/v2/authorize?{urlencode(query)}"

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        if not self.client_id or not self.client_secret:
            raise AdapterAuthError("缺少 client_id 或 client_secret，无法换取/刷新 token。")
        payload = {
            **data,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            response = await self._http().post(
                f"{self.base_url}/openapi/v2/token", data=payload,
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"请求 WorkBuddy token 接口失败：{exc}") from exc
        if response.status_code >= 400:
            raise AdapterAuthError(
                f"WorkBuddy token 接口返回 {response.status_code}：{response.text[:300]}"
            )
        token = response.json()
        self.save_token(token)
        return token

    async def exchange_code(self, code: str, state: str | None = None) -> dict[str, Any]:
        self._consume_state(state)
        return await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })

    async def refresh(self) -> dict[str, Any]:
        token = self.load_token()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise AdapterAuthError("本地没有 refresh_token，请先完成 OAuth 授权。")
        return await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        token = self.load_token()
        expires_at = float(token.get("expires_at") or 0)
        fresh = bool(token.get("access_token")) and expires_at > time.time() + 60
        if fresh and not force_refresh:
            return token["access_token"]
        if token.get("refresh_token"):
            refreshed = await self.refresh()
            return refreshed["access_token"]
        raise AdapterAuthError(
            "没有可用的 WorkBuddy access_token。请先在配置中填写 OAuth 应用信息，"
            "运行 `agentorchestrator auth url` 获取授权链接，再用 `auth exchange <code>` 换取 token。"
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
