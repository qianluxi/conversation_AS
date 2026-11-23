from __future__ import annotations

from pathlib import Path

from agentorchestrator.adapters.base import (
    AdapterError,
    AdapterTimeout,
    AgentAdapter,
    AgentReply,
    MessageRef,
)
from agentorchestrator.adapters.mock import MockAdapter
from agentorchestrator.config import MockSettings
from agentorchestrator.messaging.schema import (
    Message,
    MessageStatus,
    RunPhase,
    RunStatus,
    SessionBinding,
    Step,
    StepState,
    utc_now,
)
from agentorchestrator.orchestrator.engine import Conversation, run_collaboration
from agentorchestrator.storage.database import Store

from conftest import run


class ScriptedAdapter(AgentAdapter):
    kind = "scripted"

    def __init__(self, name: str, replies: list[str]) -> None:
        super().__init__(name)
        self.replies = list(replies)
        self.calls: list[str] = []

    async def health_check(self):
        from agentorchestrator.adapters.base import HealthStatus
        return HealthStatus(True, "scripted")

    async def create_session(self, run_id: str, config=None) -> str:
        return f"script:{self.name}"

    async def send_message(self, session_id: str, text: str, *, timeout=None) -> MessageRef:
        self.calls.append(text)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        self._last = self.replies[index]
        return MessageRef(session_id=session_id, message_id=f"{self.name}-{len(self.calls)}")

    async def wait_response(self, ref: MessageRef, *, timeout=None) -> AgentReply:
        return AgentReply(session_id=ref.session_id, text=self._last, stop_reason="end_turn")

    async def cancel(self, session_id: str) -> None:
        return None


class TimeoutWaitAdapter(MockAdapter):
    async def wait_response(self, ref: MessageRef, *, timeout=None) -> AgentReply:
        raise AdapterTimeout("模拟等待超时")


class FlakyAdapter(MockAdapter):
    def __init__(self, name: str, settings: MockSettings) -> None:
        super().__init__(name, settings)
        self.fail_next = True

    async def send_message(self, session_id: str, text: str, *, timeout=None) -> MessageRef:
        if self.fail_next:
            self.fail_next = False
            raise AdapterError("模拟发送失败（未送达）")
        return await super().send_message(session_id, text, timeout=timeout)


def test_ask_records_messages_steps_and_events(store: Store) -> None:
    conversation = Conversation(
        store, {"worker": MockAdapter("worker", MockSettings(role="worker"))}, goal="检查"
    )

    async def scenario() -> None:
        reply = await conversation.ask("worker", "请执行任务", phase=RunPhase.EXECUTING)
        assert reply.text
        await conversation.close()

    run(scenario())

    messages = store.list_messages(conversation.run_id)
    assert len(messages) == 2
    assert all(message.status is MessageStatus.COMPLETED for message in messages)
    assert messages[1].reply_to == messages[0].message_id

    steps = store.list_steps(conversation.run_id)
    assert len(steps) == 1
    assert steps[0].state is StepState.COMPLETED
    assert steps[0].started_at is not None
    assert steps[0].finished_at is not None
    assert steps[0].output_message_id == messages[1].message_id

    event_types = [event.event_type for event in store.list_events(conversation.run_id)]
    for expected in ("run_created", "step_started", "message_sending",
                     "message_acknowledged", "message_completed", "step_completed"):
        assert expected in event_types

    stored_run = store.get_run(conversation.run_id)
    assert stored_run is not None
    assert stored_run.phase is RunPhase.EXECUTING
    assert stored_run.current_turn == 1


def test_wait_timeout_marks_message_unknown(store: Store) -> None:
    adapter = TimeoutWaitAdapter("worker", MockSettings(role="worker"))
    conversation = Conversation(store, {"worker": adapter}, goal="超时")

    async def scenario() -> None:
        try:
            await conversation.ask("worker", "会超时")
        except AdapterTimeout:
            pass
        await conversation.close()

    run(scenario())

    messages = store.list_messages(conversation.run_id)
    assert messages[0].status is MessageStatus.UNKNOWN
    events = [event.event_type for event in store.list_events(conversation.run_id)]
    assert "message_unknown" in events
    steps = store.list_steps(conversation.run_id)
    assert steps[0].state is StepState.FAILED


def test_unstarted_message_is_not_resent_by_default(store: Store) -> None:
    adapter = MockAdapter("worker", MockSettings(role="worker"))
    conversation = Conversation(store, {"worker": adapter}, goal="recover")
    conversation.store.upsert_binding(
        SessionBinding(run_id=conversation.run_id, agent="worker", provider="mock",
                       provider_session_id="mock-1")
    )
    message = Message(
        run_id=conversation.run_id, from_agent="orchestrator", to_agent="worker",
        content="未发送", status=MessageStatus.CREATED,
    )
    store.save_message(message)

    results = run(conversation.resume())
    assert results[0]["action"] == "unresolved"
    assert results[0]["reason"] == "never_acknowledged"
    assert store.get_message(message.message_id).status is MessageStatus.CREATED


def test_failed_dedupe_message_can_retry_safely(store: Store) -> None:
    adapter = FlakyAdapter("worker", MockSettings(role="worker"))
    conversation = Conversation(store, {"worker": adapter}, goal="重试")

    async def scenario() -> tuple[str, str]:
        try:
            try:
                await conversation.ask("worker", "第一次", dedupe_key="retry-key")
            except AdapterError:
                pass
            failed = store.find_by_dedupe("retry-key")
            first_status = failed.status.value
            reply = await conversation.ask("worker", "第一次", dedupe_key="retry-key")
            return first_status, reply.text
        finally:
            await conversation.close()

    first_status, text = run(scenario())
    assert first_status == "failed"
    assert text
    stored = store.find_by_dedupe("retry-key")
    assert stored is not None
    assert stored.status is MessageStatus.COMPLETED
    assert stored.attempt == 2


def test_resume_reconciles_running_step_for_completed_message(store: Store) -> None:
    run_model = store.create_run("崩溃窗口修复")
    step = Step(run_id=run_model.run_id, turn_id=0, agent="worker", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    outgoing = Message(
        run_id=run_model.run_id, from_agent="orchestrator", to_agent="worker",
        content="已完成但 step 未收尾", status=MessageStatus.COMPLETED,
        metadata={"step_id": step.step_id},
    )
    store.save_message(outgoing)
    store.save_message(Message(
        run_id=run_model.run_id, from_agent="worker", to_agent="orchestrator",
        content="回复", status=MessageStatus.COMPLETED, reply_to=outgoing.message_id,
    ))
    conversation = Conversation(store, {}, run_id=run_model.run_id)

    results = run(conversation.resume())

    assert results == []
    repaired = store.get_step(step.step_id)
    assert repaired is not None
    assert repaired.state is StepState.COMPLETED
    assert repaired.output_message_id is not None
    assert "step_reconciled" in [
        event.event_type for event in store.list_events(run_model.run_id)
    ]


def test_invalid_review_does_not_approve(tmp_path: Path) -> None:
    store = Store(tmp_path / "review.db")
    adapters = {
        "planner": ScriptedAdapter("planner", ["任务方案"]),
        "worker": ScriptedAdapter("worker", ["执行完成"]),
        "reviewer": ScriptedAdapter("reviewer", ["我不认为当前修改已经达到 APPROVED 的标准。"]),
    }
    conversation = Conversation(store, adapters, goal="审查")

    async def scenario() -> dict:
        try:
            return await run_collaboration(
                conversation, planner="planner", worker="worker",
                reviewer="reviewer", max_rounds=2, goal="审查",
            )
        finally:
            await conversation.close()

    result = run(scenario())
    assert result["approved"] is False
    assert "结构非法" in (result.get("error") or "")

    stored_run = store.get_run(conversation.run_id)
    assert stored_run is not None
    assert stored_run.status is RunStatus.FAILED
    assert stored_run.phase is RunPhase.FAILED
    assert stored_run.metadata["reason"] == "invalid_review"
    store.close()
