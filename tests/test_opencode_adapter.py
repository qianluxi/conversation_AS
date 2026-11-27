from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentorchestrator.adapters.base import AdapterTimeout
from agentorchestrator.adapters.opencode import OpenCodeAdapter
from agentorchestrator.config import OpencodeSettings

from conftest import run


def _settings(**overrides: Any) -> OpencodeSettings:
    base = dict(base_url="http://oc.test", password="pw", poll_interval=0.01, timeout=3)
    base.update(overrides)
    return OpencodeSettings(**base)


def test_legacy_async_flow() -> None:
    state: dict[str, Any] = {"messages": [], "auth": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        state["auth"] = request.headers.get("authorization", "")
        path = request.url.path
        if request.method == "POST" and path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if path == "/session/ses_1/prompt_async":
            body = json.loads(request.content)
            message_id = body["messageID"]
            text = body["parts"][0]["text"]
            state["messages"] = [
                {"info": {"id": message_id, "sessionID": "ses_1", "role": "user",
                          "time": {"created": 1000}},
                 "parts": [{"type": "text", "text": text}]},
                {"info": {"id": "msg_a", "sessionID": "ses_1", "role": "assistant",
                          "parentID": message_id, "time": {"created": 2000, "completed": 3000}},
                 "parts": [{"type": "text", "synthetic": False, "text": f"reply:{text}"}]},
            ]
            return httpx.Response(204)
        if path == "/session/ses_1/message":
            return httpx.Response(200, json=state["messages"])
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if path == "/session/ses_1/abort":
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": path})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport,
                                     auth=httpx.BasicAuth("opencode", "pw")) as client:
            adapter = OpenCodeAdapter("oc", _settings(), client=client)
            health = await adapter.health_check()
            assert health.ok
            session_id = await adapter.create_session("run1")
            assert session_id == "ses_1"
            ref = await adapter.send_message(session_id, "hello")
            reply = await adapter.wait_response(ref, timeout=2)
            assert reply.text == "reply:hello"
            assert ref.message_id.startswith("msg_")
            await adapter.cancel(session_id)

    run(scenario())
    assert state["auth"].startswith("Basic ")


def test_legacy_sync_flow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/session":
            return httpx.Response(200, json={"id": "ses_sync"})
        if path == "/session/ses_sync/message":
            return httpx.Response(200, json={
                "info": {"id": "msg_a", "role": "assistant", "time": {"created": 1, "completed": 2},
                         "tokens": {"input": 1, "output": 2}},
                "parts": [{"type": "text", "text": "sync reply"}],
            })
        return httpx.Response(404, json={"error": path})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OpenCodeAdapter("oc", _settings(delivery="sync"), client=client)
            session_id = await adapter.create_session("run2")
            reply = await adapter.chat(session_id, "hi", timeout=2)
            assert reply.text == "sync reply"
            assert reply.usage["output"] == 2

    run(scenario())


def test_v2_flow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"healthy": True})
        if request.method == "POST" and path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_v2"}})
        if path == "/api/session/ses_v2/prompt":
            return httpx.Response(200, json={"data": {"id": "msg_p1"}})
        if path == "/api/session/ses_v2/wait":
            return httpx.Response(204)
        if path == "/api/session/ses_v2/message":
            if request.url.params.get("order") == "asc":
                return httpx.Response(200, json={"data": [], "cursor": {}})
            return httpx.Response(200, json={"data": [{
                "id": "msg_a", "type": "assistant",
                "content": [{"type": "text", "id": "p1", "text": "v2 reply"}],
                "time": {"created": 1, "completed": 2},
                "agent": "build", "model": {"id": "m", "providerID": "p"},
            }], "cursor": {}})
        if path == "/api/session/ses_v2/interrupt":
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404, json={"error": path})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OpenCodeAdapter("oc", _settings(api_style="v2"), client=client)
            session_id = await adapter.create_session("run3")
            ref = await adapter.send_message(session_id, "hello")
            reply = await adapter.wait_response(ref, timeout=2)
            assert reply.text == "v2 reply"
            assert reply.stop_reason == "end_turn"
            await adapter.cancel(session_id)

    run(scenario())

def test_list_models_never_leaks_provider_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/config/providers":
            return httpx.Response(200, json={"providers": [
                {"id": "p1", "name": "Provider One", "key": "SECRET-KEY",
                 "models": {"m1": {"name": "Model One"}}},
                {"id": "p2", "name": "Provider Two", "env": ["MISSING_ENV"],
                 "models": {"m2": {"name": "Model Two"}}},
            ]})
        return httpx.Response(404, json={"error": request.url.path})

    async def scenario() -> dict:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OpenCodeAdapter("oc", _settings(), client=client)
            return await adapter.list_models()

    data = run(scenario())
    text = json.dumps(data, ensure_ascii=False)
    assert "SECRET-KEY" not in text
    assert "key" not in {entry for item in data["models"] for entry in item}
    assert data["models"][0]["provider_id"] == "p1"
    assert data["models"][0]["configured"] is True
    assert data["models"][0]["model_name"] == "Model One"
    assert data["models"][1]["configured"] is False

def test_wait_timeout_aborts_remote_session() -> None:
    state: dict[str, Any] = {"messages": [], "aborted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/session":
            return httpx.Response(200, json={"id": "ses_to"})
        if path == "/session/ses_to/prompt_async":
            body = json.loads(request.content)
            state["messages"] = [
                {"info": {"id": body["messageID"], "sessionID": "ses_to", "role": "user",
                          "time": {"created": 1000}},
                 "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"id": "msg_a", "sessionID": "ses_to", "role": "assistant",
                          "parentID": body["messageID"], "time": {"created": 2000}},
                 "parts": [{"type": "tool", "tool": "bash", "state": {"status": "running"}}]},
            ]
            return httpx.Response(204)
        if path == "/session/ses_to/message":
            return httpx.Response(200, json=state["messages"])
        if path == "/session/ses_to/abort":
            state["aborted"] = True
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": path})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OpenCodeAdapter("oc", _settings(), client=client)
            session_id = await adapter.create_session("run-to")
            ref = await adapter.send_message(session_id, "hi")
            with pytest.raises(AdapterTimeout):
                await adapter.wait_response(ref, timeout=0.15)

    run(scenario())
    assert state["aborted"] is True

def test_inactivity_timeout_reports_current_tool() -> None:
    state: dict[str, Any] = {"messages": [], "aborted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/session":
            return httpx.Response(200, json={"id": "ses_idle"})
        if path == "/session/ses_idle/prompt_async":
            body = json.loads(request.content)
            state["messages"] = [
                {"info": {"id": body["messageID"], "sessionID": "ses_idle", "role": "user",
                          "time": {"created": 1000}},
                 "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"id": "msg_a", "sessionID": "ses_idle", "role": "assistant",
                          "parentID": body["messageID"], "time": {"created": 2000}},
                 "parts": [{"type": "tool", "tool": "bash",
                            "state": {"status": "running",
                                      "input": {"command": "npm install"}}}]},
            ]
            return httpx.Response(204)
        if path == "/session/ses_idle/message":
            return httpx.Response(200, json=state["messages"])
        if path == "/session/ses_idle/abort":
            state["aborted"] = True
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": path})

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OpenCodeAdapter(
                "oc", _settings(inactivity_timeout=0.08, timeout=5), client=client
            )
            session_id = await adapter.create_session("run-idle")
            ref = await adapter.send_message(session_id, "hi")
            with pytest.raises(AdapterTimeout) as excinfo:
                await adapter.wait_response(ref, timeout=5)
            assert "没有新进展" in str(excinfo.value)
            assert "npm install" in str(excinfo.value)

    run(scenario())
    assert state["aborted"] is True
