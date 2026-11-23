from __future__ import annotations

from pathlib import Path

import httpx

from agentorchestrator.adapters.base import MessageRef
from agentorchestrator.adapters.opencode import OpenCodeAdapter
from agentorchestrator.config import OpencodeSettings
from agentorchestrator.messaging.schema import (
    Message,
    MessageStatus,
    RunPhase,
    SessionBinding,
    Step,
    StepState,
    utc_now,
)
from agentorchestrator.orchestrator.engine import Conversation
from agentorchestrator.storage.database import Store

from conftest import run
from fake_servers import FakeOpenCodeHandler, LocalServer


def _prepare(tmp_path: Path, server: LocalServer, *, provider_message_id: str | None) -> tuple[Store, Store, Conversation, Message]:
    store = Store(tmp_path / "recovery.db")
    run_model = store.create_run("恢复任务")
    store.upsert_binding(SessionBinding(
        run_id=run_model.run_id, agent="opencode", provider="opencode",
        provider_session_id="ses_e2e",
    ))
    step = Step(run_id=run_model.run_id, turn_id=0, agent="opencode", role="worker",
                state=StepState.RUNNING, started_at=utc_now())
    store.create_step(step)
    message = Message(
        run_id=run_model.run_id, turn_id=0, from_agent="orchestrator", to_agent="opencode",
        content="需要恢复的任务", status=MessageStatus.ACKNOWLEDGED if provider_message_id else MessageStatus.CREATED,
        provider="opencode", provider_session_id="ses_e2e",
        provider_message_id=provider_message_id, attempt=1 if provider_message_id else 0,
        metadata={"step_id": step.step_id, "ref": {"api_style": "legacy"}},
    )
    store.save_message(message)
    adapter = OpenCodeAdapter("opencode", OpencodeSettings(
        base_url=server.url, poll_interval=0.01, timeout=5,
    ))
    conversation = Conversation(store, {"opencode": adapter}, run_id=run_model.run_id)
    return store, conversation, message


def test_recover_completed_reply_from_opencode(tmp_path: Path) -> None:
    with LocalServer(FakeOpenCodeHandler) as server:
        store, conversation, message = _prepare(
            tmp_path, server, provider_message_id="msg_user"
        )
        server.httpd.state["messages"] = [
            {"info": {"id": "msg_user", "sessionID": "ses_e2e", "role": "user",
                      "time": {"created": 1000}},
             "parts": [{"type": "text", "text": "需要恢复的任务"}]},
            {"info": {"id": "msg_assistant", "sessionID": "ses_e2e", "role": "assistant",
                      "parentID": "msg_user", "time": {"created": 2000, "completed": 3000}},
             "parts": [{"type": "text", "text": "已经执行完毕的回复"}]},
        ]

        async def scenario() -> list[dict]:
            try:
                return await conversation.resume()
            finally:
                await conversation.close()

        results = run(scenario())

        assert results[0]["action"] == "recovered"
        reloaded = store.get_message(message.message_id)
        assert reloaded is not None
        assert reloaded.status is MessageStatus.COMPLETED
        messages = store.list_messages(message.run_id)
        assert any(m.content == "已经执行完毕的回复" for m in messages)
        steps = store.list_steps(message.run_id)
        assert steps[0].state is StepState.COMPLETED
        assert steps[0].output_message_id is not None
        stored_run = store.get_run(message.run_id)
        assert stored_run is not None
        assert stored_run.phase is RunPhase.RECOVERING
        store.close()


def test_unknown_message_stays_unresolved_when_server_has_no_reply(tmp_path: Path) -> None:
    with LocalServer(FakeOpenCodeHandler) as server:
        store, conversation, message = _prepare(
            tmp_path, server, provider_message_id="msg_missing"
        )

        async def scenario() -> list[dict]:
            try:
                return await conversation.resume()
            finally:
                await conversation.close()

        results = run(scenario())
        assert results[0]["action"] == "unresolved"
        reloaded = store.get_message(message.message_id)
        assert reloaded is not None
        assert reloaded.status is MessageStatus.ACKNOWLEDGED
        store.close()


def test_resend_unstarted_message_only_when_explicit(tmp_path: Path) -> None:
    with LocalServer(FakeOpenCodeHandler) as server:
        store, conversation, message = _prepare(
            tmp_path, server, provider_message_id=None
        )

        async def scenario() -> list[dict]:
            try:
                return await conversation.resume(allow_resend_unstarted=True, timeout=5)
            finally:
                await conversation.close()

        results = run(scenario())
        assert results[0]["action"] == "resent"
        reloaded = store.get_message(message.message_id)
        assert reloaded is not None
        assert reloaded.status is MessageStatus.COMPLETED
        assert reloaded.attempt == 1
        store.close()


def _v2_handler(candidates: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_v2/message":
            return httpx.Response(200, json={"data": candidates, "cursor": {}})
        return httpx.Response(404, json={"error": request.url.path})

    return httpx.MockTransport(handler)


def test_v2_recovery_requires_unique_assistant_candidate() -> None:
    def assistant(message_id: str, text: str, finished: bool = True) -> dict:
        return {
            "id": message_id, "type": "assistant",
            "content": [{"type": "text", "id": f"{message_id}-t", "text": text}],
            "time": {"created": 1, "completed": 2} if finished else {"created": 1},
            "agent": "build", "model": {"id": "m", "providerID": "p"},
        }

    ambiguous = [assistant("msg_a", "a"), assistant("msg_b", "b")]

    async def scenario() -> tuple[object, object]:
        settings = OpencodeSettings(base_url="http://oc.test", api_style="v2",
                                    poll_interval=0.01, timeout=3)
        ref = MessageRef(session_id="ses_v2", message_id="msg_p1",
                         metadata={"api_style": "v2", "baseline_assistant_ids": []})
        async with httpx.AsyncClient(transport=_v2_handler(ambiguous)) as client:
            adapter = OpenCodeAdapter("oc", settings, client=client)
            first = await adapter.recover_reply("ses_v2", ref)
        async with httpx.AsyncClient(
            transport=_v2_handler([assistant("msg_a", "唯一的回复")])
        ) as client:
            adapter = OpenCodeAdapter("oc", settings, client=client)
            second = await adapter.recover_reply("ses_v2", ref)
        return first, second

    first, second = run(scenario())
    assert first is None
    assert second is not None
    assert second.text == "唯一的回复"
