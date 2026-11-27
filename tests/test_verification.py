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

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_capture_workspace_detects_ignored_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo-ignore"
    repo.mkdir()
    _make_repo(repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore"], cwd=repo, check=True, capture_output=True)

    before = capture_workspace(repo)
    (repo / "ignored.txt").write_text("secret change\n", encoding="utf-8")
    after = capture_workspace(repo)
    assert workspace_changed(before, after) is True


def test_read_only_unavailable_verification_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "not-git"
    workspace.mkdir()
    store = Store(tmp_path / "verify2.db")
    approved = '{"decision":"approved","summary":"ok","evidence":["e"],"confidence":0.9}'
    adapters = {
        "planner": _Scripted("planner", "任务方案"),
        "worker": _Scripted("worker", "执行完成"),
        "reviewer": _Scripted("reviewer", approved),
    }
    conversation = Conversation(store, adapters, goal="只读检查")

    async def scenario() -> dict:
        try:
            return await run_collaboration(
                conversation, planner="planner", worker="worker",
                reviewer="reviewer", max_rounds=1, goal="只读检查",
                workspace=str(workspace), read_only=True,
            )
        finally:
            await conversation.close()

    result = run(scenario())
    assert result["approved"] is False
    assert "verification_unavailable" in (result.get("error") or "")
    stored = store.get_run(conversation.run_id)
    assert stored.status is RunStatus.FAILED
    assert stored.metadata["workflow"]["error"] == "verification_unavailable"
    assert adapters["reviewer"].calls == []
    store.close()

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_content_hash_detects_same_size_and_restored_mtime(tmp_path: Path) -> None:
    import os

    repo = tmp_path / "repo-hash"
    repo.mkdir()
    _make_repo(repo)
    target = repo / "a.txt"
    target.write_text("AAAA\n", encoding="utf-8")
    before = capture_workspace(repo)
    stat = target.stat()
    target.write_text("BBBB\n", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = capture_workspace(repo)
    assert workspace_changed(before, after) is True


def test_git_command_failure_marks_unavailable(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    import agentorchestrator.verification as verification

    repo = tmp_path / "repo-fail"
    repo.mkdir()

    def fake_git(path, args, timeout=10):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        if args[:1] == ["status"]:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(verification, "_git", fake_git)
    snapshot = capture_workspace(repo)
    assert snapshot["available"] is False
    assert snapshot["reason"] == "git_command_failed"

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_symlink_target_change_is_detected(tmp_path: Path) -> None:
    import os

    repo = tmp_path / "repo-link"
    repo.mkdir()
    _make_repo(repo)
    try:
        os.symlink("a.txt", repo / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    before = capture_workspace(repo)
    (repo / "link.txt").unlink()
    try:
        os.symlink("other.txt", repo / "link.txt")
    except OSError:
        pytest.skip("symlink not supported")
    after = capture_workspace(repo)
    assert workspace_changed(before, after) is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_scan_limit_marks_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "repo-limit"
    repo.mkdir()
    _make_repo(repo)
    snapshot = capture_workspace(repo, max_file_bytes=1)
    assert snapshot["available"] is False
    assert snapshot["reason"] == "file_too_large"
    assert snapshot["files"] == 0
