from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from agentorchestrator.adapters.base import AdapterError, AdapterTimeout
from agentorchestrator.adapters.workbuddy_local import LocalAssistantGate, WorkBuddyLocalAdapter
from agentorchestrator.config import WorkBuddyLocalSettings

from conftest import FakeAuth, run
from fake_servers import FakeWorkBuddyHandler, LocalServer


def _settings(url: str, *, timeout: float = 5) -> WorkBuddyLocalSettings:
    return WorkBuddyLocalSettings(base_url=url, poll_interval=0.01, timeout=timeout)


class ToggleReplyHandler(FakeWorkBuddyHandler):
    """可通过 state["hide_reply"] 隐藏助理回复，模拟远端尚未完成。"""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if (parsed.path == "/openapi/v2/localassistant/message"
                and self.server.state.get("hide_reply")):
            self._json(200, {"code": 0, "data": {"messages": []}})
            return
        super().do_GET()


def test_local_assistant_uses_shared_gate(tmp_path: Path) -> None:
    with LocalServer(FakeWorkBuddyHandler) as server:
        gate = LocalAssistantGate()
        first = WorkBuddyLocalAdapter("wb1", _settings(server.url), auth=FakeAuth(), gate=gate)
        second = WorkBuddyLocalAdapter("wb2", _settings(server.url), auth=FakeAuth(), gate=gate)

        async def scenario() -> tuple[str, str, str]:
            ref = await first.send_message("workbuddy-local", "第一条")
            error = ""
            try:
                await second.send_message("workbuddy-local", "第二条")
            except AdapterError as exc:
                error = str(exc)
            reply = await first.wait_response(ref, timeout=5)
            ref2 = await second.send_message("workbuddy-local", "第三条")
            reply2 = await second.wait_response(ref2, timeout=5)
            await first.aclose()
            await second.aclose()
            return error, reply.text, reply2.text

        error, first_reply, second_reply = run(scenario())

        assert "禁止并发" in error
        assert first_reply
        assert second_reply


def test_timeout_quarantines_and_blocks_new_messages(tmp_path: Path) -> None:
    with LocalServer(ToggleReplyHandler) as server:
        server.httpd.state["hide_reply"] = True
        gate = LocalAssistantGate()
        adapter = WorkBuddyLocalAdapter("wb", _settings(server.url, timeout=0.2),
                                        auth=FakeAuth(), gate=gate)

        async def scenario() -> str:
            ref = await adapter.send_message("workbuddy-local", "会超时")
            try:
                await adapter.wait_response(ref, timeout=0.05)
            except AdapterTimeout:
                pass
            assert adapter.state()["phase"] == "quarantined"
            error = ""
            try:
                await adapter.send_message("workbuddy-local", "应被拒绝")
            except AdapterError as exc:
                error = str(exc)
            await adapter.release_quarantine(force=True)
            assert adapter.state()["phase"] == "idle"
            await adapter.aclose()
            return error

        error = run(scenario())
        assert "隔离状态" in error


def test_recover_reply_clears_quarantine(tmp_path: Path) -> None:
    with LocalServer(ToggleReplyHandler) as server:
        server.httpd.state["hide_reply"] = True
        adapter = WorkBuddyLocalAdapter("wb", _settings(server.url, timeout=0.2),
                                        auth=FakeAuth())

        async def scenario() -> tuple[str, str]:
            ref = await adapter.send_message("workbuddy-local", "稍后完成")
            try:
                await adapter.wait_response(ref, timeout=0.05)
            except AdapterTimeout:
                pass
            assert adapter.state()["phase"] == "quarantined"
            server.httpd.state["hide_reply"] = False
            reply = await adapter.recover_reply("workbuddy-local", ref)
            state = adapter.state()["phase"]
            await adapter.aclose()
            assert reply is not None
            return state, reply.text

        state, text = run(scenario())
        assert state == "idle"
        assert text


def test_remote_abort_is_explicitly_unsupported(tmp_path: Path) -> None:
    with LocalServer(FakeWorkBuddyHandler) as server:
        adapter = WorkBuddyLocalAdapter("wb", _settings(server.url), auth=FakeAuth())

        async def scenario() -> None:
            try:
                await adapter.send_message("workbuddy-local", "第一条")
                try:
                    await adapter.remote_abort("workbuddy-local")
                except AdapterError as exc:
                    assert "remote_abort" in str(exc)
                await adapter.cancel_wait("workbuddy-local")
                await adapter.release_quarantine(force=True)
            finally:
                await adapter.aclose()

        run(scenario())
