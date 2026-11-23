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
