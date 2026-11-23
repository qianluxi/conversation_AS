from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentorchestrator.api.server import create_app
from agentorchestrator.config import AppConfig, MockSettings, StorageSettings
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
            assert any(message["from_agent"] == "worker" for message in data["messages"])
            assert data["steps"] and all(step["state"] == "completed" for step in data["steps"])

            runs = client.get("/api/runs").json()
            assert runs and runs[0]["run_id"] == run_id
            assert client.get("/api/pending").json() == []
            assert client.get("/api/runs/does-not-exist").status_code == 404
    finally:
        store.close()
