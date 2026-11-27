from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentorchestrator.api.server import create_app
from agentorchestrator.config import AppConfig, MockSettings, StorageSettings
from agentorchestrator.messaging.schema import RunPhase, RunStatus
from agentorchestrator.storage.database import Store


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        agents={
            "planner": MockSettings(role="planner"),
            "worker": MockSettings(role="worker"),
        },
        storage=StorageSettings(path=str(tmp_path / "ui.db")),
    )


def test_ui_page_and_mock_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.storage.path)
    app = create_app(config, store)
    try:
        with TestClient(app) as client:
            page = client.get("/")
            assert page.status_code == 200
            assert "WorkBuddy" in page.text
            assert "开始协作" in page.text

            agents = client.get("/api/agents").json()
            assert {item["name"] for item in agents} == {"planner", "worker"}
            # 没有 opencode agent 时，模型列表接口应返回 400，而不是 500
            assert client.get("/api/opencode/models").status_code == 400

            health = client.get("/api/health").json()
            assert all(item["ok"] for item in health)

            response = client.post("/api/runs", json={
                "goal": "只输出 OK",
                "planner": "planner",
                "worker": "worker",
                "max_rounds": 1,
                "timeout": 10,
            })
            assert response.status_code == 200
            run_id = response.json()["run_id"]

            data = {}
            for _ in range(200):
                data = client.get(f"/api/runs/{run_id}").json()
                if not data["active"]:
                    break
                time.sleep(0.05)

            assert data["run"]["status"] == "completed"
            assert data["run"]["phase"] == "completed"
            assert data["run"]["metadata"]["approved"] is True
            assert "执行完成" in data["final_output"]
            assert any(message["from_agent"] == "worker" for message in data["messages"])
            assert data["steps"] and all(step["state"] == "completed" for step in data["steps"])

            runs = client.get("/api/runs").json()
            assert runs and runs[0]["run_id"] == run_id
            assert client.get("/api/pending").json() == []
            assert client.get("/api/runs/does-not-exist").status_code == 404
    finally:
        store.close()

def test_ui_requires_token_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.storage.path)
    app = create_app(config, store, auth_token="s3cret")
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 401
            assert client.get("/api/agents").status_code == 401
            assert client.get("/api/docs").status_code == 401
            assert client.get("/api/agents", auth=("orchestrator", "s3cret")).status_code == 200
            assert client.get(
                "/api/agents", headers={"Authorization": "Bearer s3cret"}
            ).status_code == 200
            assert client.get("/api/agents", auth=("orchestrator", "wrong")).status_code == 401
    finally:
        store.close()


def test_loopback_host_detection() -> None:
    from agentorchestrator.api.server import is_loopback_host
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")

def test_apply_opencode_overrides() -> None:
    from agentorchestrator.api.server import _apply_opencode_overrides
    from agentorchestrator.config import OpencodeSettings

    class Dummy:
        kind = "opencode"

        def __init__(self) -> None:
            self.settings = OpencodeSettings()

    dummy = Dummy()
    _apply_opencode_overrides({"oc": dummy}, "p/m", "F:/repo", "plan")
    assert dummy.settings.provider_id == "p"
    assert dummy.settings.model_id == "m"
    assert dummy.settings.directory == "F:/repo"
    assert dummy.settings.agent == "plan"


def test_start_rejects_missing_opencode_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.storage.path)
    app = create_app(config, store)
    try:
        with TestClient(app) as client:
            missing = str(tmp_path / "not-a-repo")
            response = client.post("/api/runs", json={
                "goal": "检查代码",
                "planner": "planner",
                "worker": "worker",
                "opencode_directory": missing,
            })
            assert response.status_code == 400
            assert "工作目录不存在" in response.json()["detail"]
    finally:
        store.close()

def test_continue_from_workflow_checkpoint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.storage.path)
    parent = store.create_run("检查代码")
    store.update_run(
        parent.run_id, status=RunStatus.FAILED, phase=RunPhase.FAILED,
        metadata={
            "planner": "planner", "worker": "worker", "reviewer": "planner",
            "max_rounds": 2,
            "workflow": {
                "goal": "检查代码", "planner": "planner", "worker": "worker",
                "reviewer": "planner", "max_rounds": 2, "workspace": None,
                "stage": "review", "round": 1, "plan": "任务方案",
                "work": "执行完成", "issues": "", "notes": "",
                "review": "", "review_result": None,
            },
        },
    )
    app = create_app(config, store)
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/runs/{parent.run_id}/continue", json={})
            assert response.status_code == 200
            child_id = response.json()["run_id"]
            assert response.json()["parent_run_id"] == parent.run_id

            data = {}
            for _ in range(200):
                data = client.get(f"/api/runs/{child_id}").json()
                if not data["active"]:
                    break
                time.sleep(0.05)

            assert data["run"]["status"] == "completed"
            assert data["run"]["metadata"]["approved"] is True
            assert data["run"]["metadata"]["parent_run_id"] == parent.run_id
            assert data["run"]["metadata"]["workflow"]["stage"] == "done"
            assert [step["role"] for step in data["steps"]] == ["reviewer"]
    finally:
        store.close()


def test_continue_unknown_work_requires_confirmation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.storage.path)
    parent = store.create_run("检查代码")
    store.update_run(
        parent.run_id, status=RunStatus.FAILED, phase=RunPhase.FAILED,
        metadata={
            "planner": "planner", "worker": "worker", "reviewer": "planner",
            "max_rounds": 2,
            "workflow": {
                "goal": "检查代码", "planner": "planner", "worker": "worker",
                "reviewer": "planner", "max_rounds": 2, "workspace": None,
                "stage": "work", "round": 1, "plan": "任务方案",
                "work": "", "issues": "", "notes": "",
                "review": "", "review_result": None,
            },
        },
    )
    app = create_app(config, store)
    try:
        with TestClient(app) as client:
            blocked = client.post(f"/api/runs/{parent.run_id}/continue", json={})
            assert blocked.status_code == 409
            assert "retry_unknown" in blocked.json()["detail"]

            response = client.post(
                f"/api/runs/{parent.run_id}/continue",
                json={"retry_unknown": True},
            )
            assert response.status_code == 200
            data = {}
            for _ in range(200):
                data = client.get(f"/api/runs/{response.json()['run_id']}").json()
                if not data["active"]:
                    break
                time.sleep(0.05)
            assert data["run"]["status"] == "completed"
            assert data["run"]["metadata"]["workflow"]["stage"] == "done"
    finally:
        store.close()
