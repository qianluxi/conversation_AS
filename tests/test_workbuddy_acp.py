from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentorchestrator.adapters.base import AdapterError, AdapterRemoteError, AdapterUncertain
from agentorchestrator.adapters.workbuddy_acp import AcpPhase, WorkBuddyAcpAdapter
from agentorchestrator.config import PermissionSettings, WorkBuddyAcpSettings

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

    def __init__(self, permission_title: str = "Write file",
                 prompt_status: int = 200) -> None:
        import asyncio

        self.queue: Any = asyncio.Queue()
        self.requests: list[tuple[httpx.Request, dict[str, Any]]] = []
        self.permission_title = permission_title
        self.prompt_status = prompt_status

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
            if self.prompt_status >= 400:
                return httpx.Response(self.prompt_status, json={"error": "boom"})
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
                       "params": {
                           "sessionId": "t1",
                           "toolCall": {"title": self.permission_title, "kind": "tool"},
                           "options": [
                               {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                           ],
                       }})
            self.push({"jsonrpc": "2.0", "id": body["id"],
                       "result": {"stopReason": "end_turn", "usage": {"total": 3}}})
        return httpx.Response(200, json={})


def _run_flow(*, permission_title: str = "Write file",
              auto_approve: bool = False) -> tuple[str, FakeACPTransport]:
    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
        auto_approve=auto_approve,
    )
    transport = FakeACPTransport(permission_title)
    reply_text = ""

    async def scenario() -> None:
        nonlocal reply_text
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
                reply_text = reply.text
                await adapter.cancel(session_id)
            finally:
                await adapter.aclose()

    run(scenario())
    return reply_text, transport


def _permission_result(transport: FakeACPTransport) -> dict[str, Any]:
    replies = [
        body for _request, body in transport.requests
        if body.get("id") == 99 and "result" in body
    ]
    assert replies, "没有收到对 permission 请求的应答"
    return replies[0]["result"]


def test_acp_flow_and_default_permission_denied() -> None:
    reply_text, transport = _run_flow()
    assert reply_text == "hello world"

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

    assert _permission_result(transport) == {"outcome": {"outcome": "cancelled"}}
    assert any(body.get("method") == "session/cancel" for _request, body in transport.requests)


def test_permission_allow_read_is_auto_approved() -> None:
    _reply, transport = _run_flow(permission_title="Read file features.py")
    assert _permission_result(transport) == {
        "outcome": {"outcome": "selected", "optionId": "allow"},
    }


def test_permission_deny_shell_wins_over_auto_approve() -> None:
    _reply, transport = _run_flow(permission_title="Bash shell command", auto_approve=True)
    assert _permission_result(transport) == {"outcome": {"outcome": "cancelled"}}


def test_permission_ask_respects_auto_approve() -> None:
    _reply, transport = _run_flow(permission_title="Write file", auto_approve=True)
    assert _permission_result(transport) == {
        "outcome": {"outcome": "selected", "optionId": "allow"},
    }


def test_permission_settings_categories() -> None:
    policy = PermissionSettings()
    assert policy.category("Read file") == "allow"
    assert policy.category("Write file") == "ask"
    assert policy.category("Bash: rm -rf build") == "deny"
    assert policy.category("unknown-tool") == "ask"


def test_second_prompt_before_wait_is_rejected() -> None:
    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                ref1 = await adapter.send_message(session_id, "one")
                with pytest.raises(AdapterError):
                    await adapter.send_message(session_id, "two")
                reply1 = await adapter.wait_response(ref1, timeout=3)
                assert reply1.text == "hello world"
                ref2 = await adapter.send_message(session_id, "two")
                reply2 = await adapter.wait_response(ref2, timeout=3)
                assert reply2.text == "hello world"
            finally:
                await adapter.aclose()

    run(scenario())

def test_uncertain_prompt_quarantines_session() -> None:
    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file", prompt_status=500)

    async def scenario() -> AcpPhase:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                with pytest.raises(AdapterUncertain):
                    await adapter.send_message(session_id, "one")
                session = adapter._sessions[session_id]
                assert session.state is AcpPhase.QUARANTINED
                with pytest.raises(AdapterError):
                    await adapter.send_message(session_id, "two")
                await adapter.cancel_wait(session_id)
                assert session.state is AcpPhase.CANCELLED
                return session.state
            finally:
                await adapter.aclose()

    assert run(scenario()) is AcpPhase.CANCELLED


def test_definite_prompt_error_releases_session() -> None:
    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file", prompt_status=400)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                with pytest.raises(AdapterRemoteError):
                    await adapter.send_message(session_id, "one")
                assert adapter._sessions[session_id].state is AcpPhase.IDLE
                transport.prompt_status = 200
                ref = await adapter.send_message(session_id, "two")
                reply = await adapter.wait_response(ref, timeout=3)
                assert reply.text == "hello world"
            finally:
                await adapter.aclose()

    run(scenario())

def test_permission_broker_roundtrip_with_acp() -> None:
    import asyncio

    from agentorchestrator.permissions import PermissionBroker

    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            broker = PermissionBroker(timeout=5)

            async def handler(message):
                return await broker.request("run1", "wb_acp", message)

            adapter.permission_handler = handler
            try:
                session_id = await adapter.create_session("run1")
                ref = await adapter.send_message(session_id, "hi")
                pending = []
                for _ in range(100):
                    pending = broker.pending()
                    if pending:
                        break
                    await asyncio.sleep(0.01)
                assert pending
                broker.resolve(pending[0]["request_id"], decision="allow")
                reply = await adapter.wait_response(ref, timeout=3)
                assert reply.text == "hello world"
                replies = [
                    body for _request, body in transport.requests
                    if body.get("id") == 99 and "result" in body
                ]
                assert replies
                assert replies[0]["result"]["outcome"]["optionId"] == "allow"
                assert broker.pending() == []
            finally:
                await adapter.aclose()

    run(scenario())

def test_late_chunk_after_uncertain_stays_with_old_prompt() -> None:
    import asyncio

    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file", prompt_status=500)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                with pytest.raises(AdapterUncertain):
                    await adapter.send_message(session_id, "one")
                old_state = adapter._active_by_session.get("t1")
                assert old_state is not None
                transport.push({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "t1",
                    "update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": "late"}},
                }})
                await asyncio.sleep(0.05)
                assert "late" in "".join(old_state.parts)
                with pytest.raises(AdapterError):
                    await adapter.send_message(session_id, "two")
                await adapter.cancel_wait(session_id)
            finally:
                await adapter.aclose()

    run(scenario())


def test_failed_rpc_post_clears_pending() -> None:
    import asyncio

    settings = WorkBuddyAcpSettings(
        base_url="https://control.test", bootstrap_prompt="boot",
        cwd="/workspace", timeout=3, poll_interval=0.01,
    )
    transport = FakeACPTransport("Write file")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as control, \
                httpx.AsyncClient(transport=transport) as acp:
            adapter = WorkBuddyAcpAdapter(
                "wb_acp", settings, auth=FakeAuth(),
                control_client=control, acp_client=acp,
            )
            try:
                session_id = await adapter.create_session("run1")
                task = adapter._sessions[session_id].task

                async def fail_post(payload, *, task=None, retry_auth=True):
                    raise AdapterError("post failed")

                adapter._post_acp = fail_post
                with pytest.raises(AdapterError):
                    await adapter._rpc("initialize", {}, timeout=0.1, task=task)
                assert adapter._pending_rpc == {}
            finally:
                await adapter.aclose()

    run(scenario())
