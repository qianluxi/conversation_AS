from __future__ import annotations

import json
from pathlib import Path

from agentorchestrator.adapters.opencode import OpenCodeAdapter
from agentorchestrator.adapters.workbuddy_local import WorkBuddyLocalAdapter
from agentorchestrator.config import OpencodeSettings, WorkBuddyLocalSettings
from agentorchestrator.messaging.schema import RunPhase, RunStatus, StepState
from agentorchestrator.orchestrator.engine import Conversation, run_collaboration
from agentorchestrator.storage.database import Store

from conftest import FakeAuth, run
from fake_servers import FakeOpenCodeHandler, FakeWorkBuddyHandler, LocalServer


def test_workbuddy_opencode_bidirectional(tmp_path: Path) -> None:
    with LocalServer(FakeOpenCodeHandler) as opencode_server, \
            LocalServer(FakeWorkBuddyHandler) as workbuddy_server:
        store = Store(tmp_path / "e2e.db")
        adapters = {
            "opencode": OpenCodeAdapter(
                "opencode",
                OpencodeSettings(base_url=opencode_server.url, poll_interval=0.01, timeout=10),
            ),
            "workbuddy": WorkBuddyLocalAdapter(
                "workbuddy",
                WorkBuddyLocalSettings(base_url=workbuddy_server.url, poll_interval=0.01,
                                       timeout=10),
                auth=FakeAuth(),
            ),
        }
        conversation = Conversation(store, adapters, goal="审查特征工程模块")

        async def scenario() -> dict:
            try:
                return await run_collaboration(
                    conversation, planner="workbuddy", worker="opencode",
                    max_rounds=2, goal="审查特征工程模块", timeout=10,
                )
            finally:
                await conversation.close()

        result = run(scenario())

        assert result["approved"] is True
        assert result["rounds"] == 1
        messages = store.list_messages(conversation.run_id)
        assert len(messages) == 6

        workbuddy_messages = [m for m in messages if m.from_agent == "workbuddy"]
        opencode_messages = [m for m in messages if m.from_agent == "opencode"]
        assert len(workbuddy_messages) == 2
        assert len(opencode_messages) == 1
        reviews = [json.loads(m.content) for m in workbuddy_messages
                   if m.content.lstrip().startswith("{")]
        assert reviews and reviews[0]["decision"] == "approved"
        assert "OPENCODE 执行结果" in opencode_messages[0].content

        assert all(message.status.value == "completed" for message in messages)
        stored_run = store.get_run(conversation.run_id)
        assert stored_run is not None
        assert stored_run.status is RunStatus.COMPLETED
        assert stored_run.phase is RunPhase.APPROVED
        assert stored_run.metadata["approved"] is True
        assert store.list_events(conversation.run_id)
        steps = store.list_steps(conversation.run_id)
        assert steps and all(step.state is StepState.COMPLETED for step in steps)
        store.close()


def test_ask_dedupe_returns_stored_reply(tmp_path: Path) -> None:
    with LocalServer(FakeWorkBuddyHandler) as workbuddy_server:
        store = Store(tmp_path / "dedupe.db")
        adapter = WorkBuddyLocalAdapter(
            "workbuddy",
            WorkBuddyLocalSettings(base_url=workbuddy_server.url, poll_interval=0.01, timeout=10),
            auth=FakeAuth(),
        )
        conversation = Conversation(store, {"workbuddy": adapter}, goal="dedupe")

        async def scenario() -> tuple[str, str, bool]:
            try:
                first = await conversation.ask("workbuddy", "分析需求",
                                               dedupe_key="fixed-key", timeout=10)
                second = await conversation.ask("workbuddy", "分析需求",
                                                dedupe_key="fixed-key", timeout=10)
                return first.text, second.text, bool(second.metadata.get("deduplicated"))
            finally:
                await conversation.close()

        first_text, second_text, deduplicated = run(scenario())
        assert first_text == second_text
        assert deduplicated is True
        assert len(store.list_messages(conversation.run_id)) == 2
        store.close()
