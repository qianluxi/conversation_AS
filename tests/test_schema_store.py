from __future__ import annotations

import pytest

from agentorchestrator.messaging.schema import Message, MessageStatus, RunStatus
from agentorchestrator.storage.database import Store


def test_message_roundtrip_and_indexes(store: Store) -> None:
    run = store.create_run("检查特征工程")
    outgoing = Message(
        run_id=run.run_id, turn_id=1, from_agent="orchestrator", to_agent="opencode",
        content="请检查数据泄漏", status=MessageStatus.PENDING, provider="opencode",
        provider_session_id="ses_1", dedupe_key="run:1:oc",
    )
    store.save_message(outgoing)
    store.update_message(outgoing.message_id, status=MessageStatus.SENT,
                         provider_message_id="msg_provider")

    loaded = store.get_message(outgoing.message_id)
    assert loaded is not None
    assert loaded.status is MessageStatus.SENT
    assert loaded.provider_message_id == "msg_provider"

    reply = Message(
        run_id=run.run_id, turn_id=1, from_agent="opencode", to_agent="orchestrator",
        content="发现 2 个问题", status=MessageStatus.COMPLETED,
        reply_to=outgoing.message_id,
    )
    store.save_message(reply)

    assert store.find_by_dedupe(run.run_id, "run:1:oc").message_id == outgoing.message_id
    assert store.find_reply(outgoing.message_id).content == "发现 2 个问题"
    assert [m.message_id for m in store.list_messages(run.run_id)] == [
        outgoing.message_id, reply.message_id,
    ]

    journal_mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.lower() == "wal"


def test_dedupe_key_is_unique(store: Store) -> None:
    store.create_run("goal")
    first = Message(run_id="run_x", from_agent="a", to_agent="b", content="1", dedupe_key="dup")
    store.save_message(first)
    second = Message(run_id="run_x", from_agent="a", to_agent="b", content="2", dedupe_key="dup")
    with pytest.raises(Exception):
        store.save_message(second)


def test_session_binding_upsert(store: Store) -> None:
    from agentorchestrator.messaging.schema import SessionBinding

    run = store.create_run("goal")
    binding = SessionBinding(run_id=run.run_id, agent="opencode", provider="opencode",
                             provider_session_id="ses_1")
    store.upsert_binding(binding)
    binding.provider_session_id = "ses_2"
    store.upsert_binding(binding)
    loaded = store.get_binding(run.run_id, "opencode")
    assert loaded is not None
    assert loaded.provider_session_id == "ses_2"


def test_run_status_update(store: Store) -> None:
    run = store.create_run("goal")
    store.update_run(run.run_id, RunStatus.COMPLETED, metadata={"approved": True})
    loaded = store.get_run(run.run_id)
    assert loaded is not None
    assert loaded.status is RunStatus.COMPLETED
    assert loaded.metadata["approved"] is True

def test_dedupe_key_is_scoped_per_run(store: Store) -> None:
    run_a = store.create_run("a")
    run_b = store.create_run("b")
    first = Message(run_id=run_a.run_id, from_agent="orchestrator", to_agent="x",
                    content="1", dedupe_key="same-key")
    second = Message(run_id=run_b.run_id, from_agent="orchestrator", to_agent="x",
                     content="2", dedupe_key="same-key")
    store.save_message(first)
    store.save_message(second)

    assert store.find_by_dedupe(run_a.run_id, "same-key").message_id == first.message_id
    assert store.find_by_dedupe(run_b.run_id, "same-key").message_id == second.message_id
