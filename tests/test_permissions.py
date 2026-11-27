from __future__ import annotations

from agentorchestrator.config import PermissionSettings, tokenize_tool


def test_substring_matching_is_not_used() -> None:
    policy = PermissionSettings()
    assert policy.category("spreadsheet.write") == "ask"
    assert policy.category("create_thread") == "ask"
    assert policy.category("read_file") == "allow"
    assert policy.category("execute_command") == "deny"
    assert policy.category("file.remove") == "deny"
    assert policy.category("Write file") == "ask"
    assert policy.category("Bash: rm -rf build") == "deny"
    assert policy.category("unknown-tool") == "ask"


def test_compound_operations_take_strictest() -> None:
    policy = PermissionSettings()
    assert policy.category("read write file") == "ask"
    assert policy.category("read delete file") == "deny"
    capability = policy.classify("spreadsheet.write")
    assert capability.operations == {"write"}
    assert capability.category == "ask"


def test_camel_case_and_separators_are_tokenized() -> None:
    assert tokenize_tool("readFile") == {"read", "file"}
    assert tokenize_tool("spreadsheet.write") == {"spreadsheet", "write"}
    assert tokenize_tool("Bash: rm -rf") == {"bash", "rm", "rf"}


def test_custom_policy_can_override_defaults() -> None:
    policy = PermissionSettings(allow=["write"], deny=["read"])
    assert policy.category("write_file") == "allow"
    assert policy.category("read_file") == "deny"


def test_conflicting_policy_deny_wins() -> None:
    policy = PermissionSettings(allow=["write"], ask=[], deny=["write"])
    assert policy.category("write_file") == "deny"

def test_select_option_prefers_allow_once() -> None:
    from agentorchestrator.permissions import select_option

    options = [
        {"optionId": "always", "kind": "allow_always"},
        {"optionId": "once", "kind": "allow_once"},
    ]
    assert select_option(options) == {
        "outcome": {"outcome": "selected", "optionId": "once"},
    }
    assert select_option(options, kinds=("allow_once",)) == {
        "outcome": {"outcome": "selected", "optionId": "once"},
    }


def test_broker_resolve_and_timeout() -> None:
    import asyncio

    from agentorchestrator.permissions import PermissionBroker

    async def resolved() -> dict:
        broker = PermissionBroker(timeout=5)
        message = {"params": {"toolCall": {"title": "Write file"}, "options": [
            {"optionId": "once", "kind": "allow_once", "name": "Once"},
        ]}}
        task = asyncio.create_task(broker.request("run1", "agent", message))
        await asyncio.sleep(0.01)
        pending = broker.pending("run1")
        assert len(pending) == 1
        broker.resolve(pending[0]["request_id"], decision="allow")
        result = await task
        assert result["outcome"]["optionId"] == "once"
        assert broker.pending() == []
        return result

    from conftest import run
    run(resolved())

    async def timeout() -> dict:
        broker = PermissionBroker(timeout=0.02)
        message = {"params": {"options": [{"optionId": "once", "kind": "allow_once"}]}}
        return await broker.request("run2", "agent", message)

    assert run(timeout()) == {"outcome": {"outcome": "cancelled"}}

def test_broker_cancel_run_returns_cancelled() -> None:
    import asyncio

    from agentorchestrator.permissions import PermissionBroker

    async def scenario() -> dict:
        broker = PermissionBroker(timeout=5)
        message = {"params": {"options": [{"optionId": "once", "kind": "allow_once"}]}}
        task = asyncio.create_task(broker.request("run-cancel", "agent", message))
        await asyncio.sleep(0.01)
        broker.cancel_run("run-cancel")
        return await task

    from conftest import run
    assert run(scenario()) == {"outcome": {"outcome": "cancelled"}}
