from __future__ import annotations

import asyncio
from pathlib import Path

from agentorchestrator.adapters.base import AdapterError
from agentorchestrator.adapters.workbuddy_local import WorkBuddyLocalAdapter
from agentorchestrator.config import WorkBuddyLocalSettings

from conftest import FakeAuth, run
from fake_servers import FakeWorkBuddyHandler, LocalServer


def _settings(url: str) -> WorkBuddyLocalSettings:
    return WorkBuddyLocalSettings(base_url=url, poll_interval=0.01, timeout=5)


def test_local_assistant_uses_shared_global_lock(tmp_path: Path) -> None:
    with LocalServer(FakeWorkBuddyHandler) as server:
        shared = asyncio.Lock()
        first = WorkBuddyLocalAdapter("wb1", _settings(server.url), auth=FakeAuth(), lock=shared)
        second = WorkBuddyLocalAdapter("wb2", _settings(server.url), auth=FakeAuth(), lock=shared)

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

        assert "全局互斥" in error
        assert first_reply
        assert second_reply


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
            finally:
                await adapter.aclose()

        run(scenario())
