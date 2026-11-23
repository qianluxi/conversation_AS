from __future__ import annotations

from agentorchestrator.adapters.workbuddy_ipc import WorkBuddyIpcAdapter
from agentorchestrator.config import WorkBuddyIpcSettings

from conftest import run


def test_parse_sse_extracts_content_and_usage() -> None:
    text = (
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"K"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":""}}],"usage":{"total_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    content, reasoning, usage = WorkBuddyIpcAdapter.parse_sse(text)
    assert content == "OK"
    assert reasoning == ""
    assert usage == {"total_tokens": 3}


class FakeChat:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append([dict(item) for item in messages])
        return f"回复{len(self.calls)}"


def test_ipc_adapter_keeps_per_session_history() -> None:
    fake = FakeChat()
    settings = WorkBuddyIpcSettings(history_limit=2)
    adapter = WorkBuddyIpcAdapter("workbuddy", settings, chat_fn=fake)

    async def scenario() -> tuple[str, str, str, object]:
        session_id = await adapter.create_session("run1")
        first = await adapter.chat(session_id, "第一个问题")
        second = await adapter.chat(session_id, "第二个问题")
        other_session = await adapter.create_session("run2")
        third = await adapter.chat(other_session, "新会话")
        health = await adapter.health_check()
        await adapter.aclose()
        return first.text, second.text, third.text, health

    first, second, third, health = run(scenario())
    assert first == "回复1"
    assert second == "回复2"
    assert third == "回复3"
    assert health.ok

    # 第二次调用应携带上一轮 user/assistant
    assert fake.calls[1][0]["content"] == "第一个问题"
    assert fake.calls[1][1]["content"] == "回复1"
    assert fake.calls[1][2]["content"] == "第二个问题"
    # 新 run 的会话不应看到 run1 的历史
    assert fake.calls[2] == [{"role": "user", "content": "新会话"}]
