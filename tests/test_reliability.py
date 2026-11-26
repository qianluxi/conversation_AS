from __future__ import annotations

import sqlite3

import pytest

from agentorchestrator.adapters.base import AdapterError, AdapterUncertain
from agentorchestrator.adapters.mock import MockAdapter
from agentorchestrator.config import MockSettings
from agentorchestrator.messaging.schema import (
    InvalidTransition,
    Message,
    MessageStatus,
    RunPhase,
    RunStatus,
    SessionBinding,
    Step,
    StepState,
    utc_now,
)
from agentorchestrator.orchestrator.engine import Conversation
from agentorchestrator.storage.database import Store

from conftest import run


class UncertainAdapter(MockAdapter):
    async def send_message(self, session_id: str, text: str, *, timeout=None):
        raise AdapterUncertain("模拟 WriteTimeout：请求可能已被接受")


class LocalKindAdapter(MockAdapter):
    kind = "workbuddy_local"


def test_uncertain_send_marks_unknown_not_failed(store: Store) -> None:
    conversation = Conversation(
        store, {"worker": UncertainAdapter("worker", MockSettings())}, goal="uncertain"
    )

    async def scenario() -> None:
        try:
            await conversation.ask("worker", "hi")
        except AdapterUncertain:
            pass
        await conversation.close()

    run(scenario())

    messages = store.list_messages(conversation.run_id)
    assert messages[0].status is MessageStatus.UNKNOWN
    event_types = [event.event_type for event in store.list_events(conversation.run_id)]
    assert "message_unknown" in event_types
    assert "message_failed" not in event_types


def test_terminal_run_cannot_continue_or_transition(store: Store) -> None:
    run_model = store.create_run("终态")
    store.transition_run(
        run_model.run_id, status=RunStatus.COMPLETED, phase=RunPhase.COMPLETED,
        event_type="run_completed", validate=False,
    )
    with pytest.raises(InvalidTransition):
        store.transition_run(run_model.run_id, phase=RunPhase.EXECUTING,
                             event_type="invalid")

    conversation = Conversation(
        store, {"worker": MockAdapter("worker", MockSettings())}, run_id=run_model.run_id
    )

    async def scenario() -> None:
        try:
            await conversation.ask("worker", "hi")
        except RuntimeError:
            pass
        await conversation.close()

    run(scenario())
    assert store.list_messages(run_model.run_id) == []


def test_invalid_phase_transition_is_rejected(store: Store) -> None:
    run_model = store.create_run("迁移")
    store.transition_run(run_model.run_id, phase=RunPhase.ANALYZING, event_type="a")
    store.transition_run(run_model.run_id, phase=RunPhase.EXECUTING, event_type="b")
    store.transition_run(run_model.run_id, phase=RunPhase.REVIEWING, event_type="c")
    with pytest.raises(InvalidTransition):
        store.transition_run(run_model.run_id, phase=RunPhase.ANALYZING, event_type="d")


def test_complete_exchange_rolls_back_on_failure(store: Store) -> None:
    run_model = store.create_run("原子性")
    step = Step(run_id=run_model.run_id, agent="worker", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    outgoing = Message(
        run_id=run_model.run_id, from_agent="orchestrator", to_agent="worker",
        content="任务", status=MessageStatus.ACKNOWLEDGED,
        metadata={"step_id": step.step_id},
    )
    store.save_message(outgoing)
    bad_reply = Message(
        message_id=outgoing.message_id,  # 主键冲突，强制事务回滚
        run_id=run_model.run_id, from_agent="worker", to_agent="orchestrator",
        content="回复", status=MessageStatus.COMPLETED, reply_to=outgoing.message_id,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_exchange(
            outgoing_message_id=outgoing.message_id, reply=bad_reply,
            step_id=step.step_id, run_id=run_model.run_id, current_turn=1,
        )

    assert store.get_message(outgoing.message_id).status is MessageStatus.ACKNOWLEDGED
    assert store.get_step(step.step_id).state is StepState.RUNNING
    assert not [event for event in store.list_events(run_model.run_id)
                if event.event_type == "message_completed"]


def test_complete_exchange_is_consistent_when_successful(store: Store) -> None:
    run_model = store.create_run("原子成功")
    step = Step(run_id=run_model.run_id, agent="worker", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    outgoing = Message(
        run_id=run_model.run_id, from_agent="orchestrator", to_agent="worker",
        content="任务", status=MessageStatus.ACKNOWLEDGED,
        metadata={"step_id": step.step_id},
    )
    store.save_message(outgoing)
    reply = Message(
        run_id=run_model.run_id, from_agent="worker", to_agent="orchestrator",
        content="回复", status=MessageStatus.COMPLETED, reply_to=outgoing.message_id,
    )
    store.complete_exchange(
        outgoing_message_id=outgoing.message_id, reply=reply,
        step_id=step.step_id, run_id=run_model.run_id, current_turn=1,
    )

    assert store.get_message(outgoing.message_id).status is MessageStatus.COMPLETED
    assert store.get_step(step.step_id).state is StepState.COMPLETED
    assert store.get_step(step.step_id).output_message_id == reply.message_id
    assert store.get_run(run_model.run_id).current_turn == 1
    event_types = [event.event_type for event in store.list_events(run_model.run_id)]
    assert "message_completed" in event_types
    assert "step_completed" in event_types


def test_engine_blocks_new_local_message_while_unknown_exists(store: Store) -> None:
    previous = store.create_run("旧任务")
    store.save_message(Message(
        run_id=previous.run_id, from_agent="orchestrator", to_agent="workbuddy_local",
        content="旧消息", status=MessageStatus.UNKNOWN, provider="workbuddy_local",
    ))
    conversation = Conversation(
        store, {"workbuddy_local": LocalKindAdapter("workbuddy_local", MockSettings())},
        goal="新任务",
    )

    async def scenario() -> str:
        try:
            await conversation.ask("workbuddy_local", "新消息")
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        finally:
            await conversation.close()
        return ""

    error = run(scenario())
    assert "未确认的远端任务" in error or "禁止新消息" in error


def test_release_requires_confirmation_or_force(store: Store) -> None:
    run_model = store.create_run("释放")
    store.upsert_binding(SessionBinding(
        run_id=run_model.run_id, agent="worker", provider="mock",
        provider_session_id="mock-1",
    ))
    step = Step(run_id=run_model.run_id, turn_id=0, agent="worker", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    message = Message(
        run_id=run_model.run_id, from_agent="orchestrator", to_agent="worker",
        content="结果未知", status=MessageStatus.UNKNOWN, provider="mock",
        metadata={"step_id": step.step_id},
    )
    store.save_message(message)
    conversation = Conversation(
        store, {"worker": MockAdapter("worker", MockSettings())}, run_id=run_model.run_id
    )

    async def scenario() -> Message:
        try:
            with pytest.raises(AdapterError):
                await conversation.release(message.message_id)
            return await conversation.release(message.message_id, force=True)
        finally:
            await conversation.close()

    released = run(scenario())
    assert released.status is MessageStatus.CANCELLED
    assert store.get_step(step.step_id).state is StepState.FAILED
    assert store.list_unfinished() == []
    assert "message_released" in [
        event.event_type for event in store.list_events(run_model.run_id)
    ]

def test_release_rejects_message_from_another_run(store: Store) -> None:
    run_a = store.create_run("a")
    run_b = store.create_run("b")
    message = Message(run_id=run_a.run_id, from_agent="orchestrator",
                      to_agent="worker", content="x", status=MessageStatus.UNKNOWN)
    store.save_message(message)
    conversation = Conversation(
        store, {"worker": MockAdapter("worker", MockSettings())}, run_id=run_b.run_id
    )

    async def scenario() -> None:
        try:
            await conversation.release(message.message_id, force=True)
        finally:
            await conversation.close()

    with pytest.raises(ValueError):
        run(scenario())
    assert store.get_message(message.message_id).status is MessageStatus.UNKNOWN


def test_complete_exchange_rejects_cross_run_reply(store: Store) -> None:
    run_a = store.create_run("a")
    run_b = store.create_run("b")
    step = Step(run_id=run_a.run_id, agent="worker", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    outgoing = Message(run_id=run_a.run_id, from_agent="orchestrator",
                       to_agent="worker", content="task",
                       status=MessageStatus.ACKNOWLEDGED,
                       metadata={"step_id": step.step_id})
    store.save_message(outgoing)
    reply = Message(run_id=run_b.run_id, from_agent="worker",
                    to_agent="orchestrator", content="reply",
                    status=MessageStatus.COMPLETED, reply_to=outgoing.message_id)
    with pytest.raises(ValueError):
        store.complete_exchange(
            outgoing_message_id=outgoing.message_id, reply=reply,
            step_id=step.step_id, run_id=run_a.run_id, current_turn=1,
        )
    assert store.get_message(outgoing.message_id).status is MessageStatus.ACKNOWLEDGED
    assert store.get_step(step.step_id).state is StepState.RUNNING


def test_terminal_run_cannot_retry_failed_dedupe(store: Store) -> None:
    conversation = Conversation(
        store, {"worker": MockAdapter("worker", MockSettings())}, goal="terminal retry"
    )
    store.save_message(Message(
        run_id=conversation.run_id, from_agent="orchestrator", to_agent="worker",
        content="x", status=MessageStatus.FAILED, dedupe_key="terminal-key",
    ))

    async def scenario() -> None:
        try:
            conversation.finish(RunStatus.FAILED, RunPhase.FAILED)
            with pytest.raises(RuntimeError):
                await conversation.ask("worker", "x", dedupe_key="terminal-key")
        finally:
            await conversation.close()

    run(scenario())
