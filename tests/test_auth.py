from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from agentorchestrator.adapters.base import AdapterAuthError
from agentorchestrator.auth import WorkBuddyAuth

from conftest import run


def _auth(tmp_path: Path, client: httpx.AsyncClient | None = None) -> WorkBuddyAuth:
    return WorkBuddyAuth(
        base_url="https://wb.test",
        client_id="client",
        client_secret="secret",
        redirect_uri="http://127.0.0.1:8765/callback",
        token_file=str(tmp_path / "token.json"),
        scope="user.localassistant.readable",
        client=client,
    )


def test_begin_authorization_persists_state(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    url, state = auth.begin_authorization("known-state")
    assert "state=known-state" in url
    assert auth.state_file.exists()
    saved = json.loads(auth.state_file.read_text(encoding="utf-8"))
    assert saved["state"] == "known-state"


def test_exchange_rejects_wrong_or_missing_state(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    auth.begin_authorization("expected")

    with pytest.raises(AdapterAuthError):
        run(auth.exchange_code("code", "wrong"))
    with pytest.raises(AdapterAuthError):
        run(auth.exchange_code("code", None))
    # state 不被错误尝试消耗，仍可重试
    assert auth.state_file.exists()


def test_exchange_rejects_expired_state(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    auth.begin_authorization("expired")
    data = json.loads(auth.state_file.read_text(encoding="utf-8"))
    data["created_at"] = time.time() - auth.STATE_TTL_SECONDS - 10
    auth.state_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AdapterAuthError):
        run(auth.exchange_code("code", "expired"))
    assert not auth.state_file.exists()


def test_exchange_with_valid_state_saves_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi/v2/token"
        return httpx.Response(200, json={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": "user.localassistant.readable",
            "open_id": "op_1",
        })

    transport = httpx.MockTransport(handler)
    async def scenario() -> dict:
        async with httpx.AsyncClient(transport=transport) as client:
            auth = _auth(tmp_path, client=client)
            _url, state = auth.begin_authorization("good")
            token = await auth.exchange_code("code", state)
            status = auth.status()
            return {"token": token, "status": status, "state_exists": auth.state_file.exists()}

    result = run(scenario())
    assert result["token"]["access_token"] == "access"
    assert result["status"]["has_access_token"] is True
    assert result["status"]["has_refresh_token"] is True
    assert result["state_exists"] is False
