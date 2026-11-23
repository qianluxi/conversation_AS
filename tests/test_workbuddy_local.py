from __future__ import annotations

import json
from typing import Any

import httpx

from agentorchestrator.adapters.workbuddy_local import WorkBuddyLocalAdapter
from agentorchestrator.config import WorkBuddyLocalSettings

from conftest import FakeAuth, run


def _settings() -> WorkBuddyLocalSettings:
    return WorkBuddyLocalSettings(
        base_url="https://wb.test", poll_interval=0.01, timeout=3,
        token_file="ignored-token.json",
    )


def test_local_assistant_flow() -> None:
    state: dict[str, Any] = {"auth": "", "content": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/openapi/v2/localassistant":
            state["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"code": 0, "data": {"online": True}})
        if path == "/openapi/v2/localassistant/message" and request.method == "POST":
            state["auth"] = request.headers.get("authorization", "")
            state["content"] = json.loads(request.content)["content"]
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "wb-1"}})
        if path == "/openapi/v2/localassistant/message" and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"messages": [
                {"message_id": "wb-1", "role": "user", "content": [state["content"]]},
                {"message_id": "wb-1-a", "role": "assistant", "content": ["日程：3 个会议"]},
            ]}})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    async def scenario() -> None:
        auth = FakeAuth()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = WorkBuddyLocalAdapter("wb", _settings(), auth=auth, client=client)
            health = await adapter.health_check()
            assert health.ok
            session_id = await adapter.create_session("run1")
            reply = await adapter.chat(session_id, "帮我查一下今天的日程", timeout=2)
            assert reply.text == "日程：3 个会议"

    run(scenario())
    assert state["auth"] == "Bearer test-token"
    assert state["content"] == "帮我查一下今天的日程"


def test_local_assistant_refreshes_on_401() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi/v2/localassistant/message" and request.method == "POST":
            state["calls"] += 1
            if state["calls"] == 1:
                return httpx.Response(401, json={"code": 401, "msg": "expired"})
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "wb-2"}})
        if request.url.path == "/openapi/v2/localassistant/message":
            return httpx.Response(200, json={"code": 0, "data": {"messages": [
                {"message_id": "wb-2-a", "role": "assistant", "content": ["ok"]},
            ]}})
        return httpx.Response(404, json={"code": 404})

    async def scenario() -> None:
        auth = FakeAuth()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = WorkBuddyLocalAdapter("wb", _settings(), auth=auth, client=client)
            ref = await adapter.send_message("workbuddy-local", "hi")
            assert ref.message_id == "wb-2"
            reply = await adapter.wait_response(ref, timeout=2)
            assert reply.text == "ok"
            assert auth.refreshed == 1

    run(scenario())
