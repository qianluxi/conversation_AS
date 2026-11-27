from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentorchestrator.adapters.base import AgentAdapter, AgentReply, MessageRef
from agentorchestrator.messaging.schema import RunStatus
from agentorchestrator.orchestrator.engine import Conversation, run_collaboration
from agentorchestrator.storage.database import Store
from agentorchestrator.verification import (
    capture_workspace,
    workspace_changed,
)

from conftest import run


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


class _Scripted(AgentAdapter):
    kind = "test"

    def __init__(self, name: str, reply: str) -> None:
        super().__init__(name)
        self.reply = reply
        self.calls: list[str] = []

    async def health_check(self):
        from agentorchestrator.adapters.base import HealthStatus
        return HealthStatus(True)

    async def create_session(self, run_id: str, config=None) -> str:
        return f"test-{self.name}"

    async def send_message(self, session_id: str, text: str, *, timeout=None) -> MessageRef:
        self.calls.append(text)
        return MessageRef(session_id=session_id, message_id=f"{self.name}-{len(self.calls)}")

    async def wait_response(self, ref: MessageRef, *, timeout=None) -> AgentReply:
        return AgentReply(session_id=ref.session_id, text=self.reply, stop_reason="end_turn")

    async def cancel(self, session_id: str) -> None:
        return None


class _WritingWorker(_Scripted):
    def __init__(self, name: str, path: Path) -> None:
        super().__init__(name, "执行完成")
        self.path = path

    async def send_message(self, session_id: str, text: str, *, timeout=None) -> MessageRef:
        (self.path / "a.txt").write_text("changed by worker\n", encoding="utf-8")
        return await super().send_message(session_id, text, timeout=timeout)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_capture_workspace_detects_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    before = capture_workspace(repo)
    assert before["available"] is True
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    after = capture_workspace(repo)
    assert workspace_changed(before, after) is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_read_only_workflow_fails_on_workspace_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    store = Store(tmp_path / "verify.db")
    approved = json.dumps({
        "decision": "approved", "summary": "ok", "findings": [],
        "evidence": ["reviewed"], "confidence": 0.95,
    })
    adapters = {
        "planner": _Scripted("planner", "任务方案"),
        "worker": _WritingWorker("worker", repo),
        "reviewer": _Scripted("reviewer", approved),
    }
    conversation = Conversation(store, adapters, goal="只读检查")

    async def scenario() -> dict:
        try:
            return await run_collaboration(
                conversation, planner="planner", worker="worker",
                reviewer="reviewer", max_rounds=1, goal="只读检查",
                workspace=str(repo), read_only=True,
            )
        finally:
            await conversation.close()

    result = run(scenario())
    assert result["approved"] is False
    assert "只读" in (result.get("error") or "")
    stored = store.get_run(conversation.run_id)
    assert stored.status is RunStatus.FAILED
    assert stored.metadata["workflow"]["stage"] == "failed"
    assert stored.metadata["workflow"]["verification"]["changed"] is True
    assert adapters["reviewer"].calls == []
    store.close()


def test_capture_workspace_non_git(tmp_path: Path) -> None:
    snapshot = capture_workspace(tmp_path)
    assert snapshot["available"] is False
