from __future__ import annotations

import json
from typing import Any

import httpx

from agentorchestrator.adapters.workbuddy_acp import WorkBuddyAcpAdapter
from agentorchestrator.config import WorkBuddyAcpSettings

from conftest import FakeAuth, run


def _sse(payload: dict[str, Any]) -> bytes:
    return f"event: message\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue: "Any") -> None:
        self.queue = queue

    async def __aiter__(self) -> Any:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        return None


class FakeACPTransport(httpx.AsyncBaseTransport):
    """模拟 WorkBuddy 控制面 + ACP SSE 通道。"""

    def __init__(self) -> None:
        import asyncio

        self.queue: Any = asyncio.Queue()
        self.requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    def push(self, payload: dict[str, Any]) -> None:
        self.queue.put_nowait(_sse(payload))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/openapi/v2/tasks"):
            return httpx.Response(200, json={"tasks": [], "total": 0})
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "acp-connection-id": "conn-1"},
                stream=_QueueStream(self.queue),
            )

        body = json.loads(request.content or b"{}")
        self.requests.append((request, body))
        if path.endswith("/openapi/v2/tasks"):
            return httpx.Response(200, json={
                "task_id": "t1", "status": "idle",
                "link": "https://acp.test/sessions/t1", "token": "tok1",
            })
        if path.startswith("/openapi/v2/tasks/"):
            return httpx.Response(200, json={
                "task_id": "t1", "link": "https://acp.test/sessions/t1", "token": "tok1",
            })

        method = body.get("method")
        if method == "initialize":
            self.push({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": 1}})
        elif method == "session/load":
            self.push({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        elif method == "session/prompt":
            self.push({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "t1",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "hello "}},
            }})
            self.push({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "t1",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "world"}},
            }})
            self.push({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                       "params": {"sessionId": "t1", "options": [
                           {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                       ]}})
            self.push({"jsonrpc": "2.0", "id": body["id"],
                       "result": {"stopReason": "end_turn", "usage": {"total": 3}}})
        return httpx.Response(200, json={})


def test_acp_flow() -> None:
    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                ref = await adapter.send_message(session_id, "hi")
                reply = await adapter.wait_response(ref, timeout=3)
                assert reply.text == "hello world"
                assert reply.stop_reason == "end_turn"
                assert reply.usage == {"total": 3}
                await adapter.cancel(session_id)
            finally:
                await adapter.aclose()

    run(scenario())

    methods = [body.get("method") for _request, body in transport.requests]
    assert methods.count("initialize") == 1
    assert methods.count("session/load") == 1
    assert methods.count("session/prompt") == 1

    prompt_request, prompt_body = next(
        (request, body) for request, body in transport.requests
        if body.get("method") == "session/prompt"
    )
    assert prompt_body["params"]["prompt"][0]["text"] == "hi"
    assert prompt_request.headers["authorization"] == "Bearer tok1"
    assert prompt_request.headers["acp-connection-id"] == "conn-1"

    permission_replies = [
        body for _request, body in transport.requests
        if body.get("id") == 99 and "result" in body
    ]
    assert permission_replies
    assert permission_replies[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    assert any(body.get("method") == "session/cancel" for _request, body in transport.requests)
