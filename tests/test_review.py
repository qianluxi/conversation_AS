from __future__ import annotations

import pytest

from agentorchestrator.review import ReviewParseError, parse_review_result


def test_parse_plain_json() -> None:
    result = parse_review_result(
        '{"decision": "approved", "summary": "ok", "findings": [], '
        '"evidence": ["pytest"], "confidence": 0.9}'
    )
    assert result.approved
    assert result.confidence == 0.9


def test_parse_fenced_json() -> None:
    text = """审查如下：
```json
{"decision": "revision_required", "summary": "缺少测试",
 "findings": [{"severity": "high", "file": "a.py", "problem": "未覆盖异常分支",
               "suggested_action": "补充测试"}],
 "evidence": ["pytest -q"], "confidence": 0.8}
```
"""
    result = parse_review_result(text)
    assert result.decision == "revision_required"
    assert result.findings[0].severity.value == "high"


def test_approval_word_in_negation_is_rejected() -> None:
    with pytest.raises(ReviewParseError):
        parse_review_result("我不认为当前修改已经达到 APPROVED 的标准。")


def test_invalid_decision_is_rejected() -> None:
    with pytest.raises(ReviewParseError):
        parse_review_result('{"decision": "looks_good", "summary": "x"}')


def test_confidence_out_of_range_is_rejected() -> None:
    with pytest.raises(ReviewParseError):
        parse_review_result('{"decision": "approved", "confidence": 1.7}')


def test_wrapped_review_object_is_accepted() -> None:
    result = parse_review_result('{"review": {"decision": "blocked", "summary": "信息不足"}}')
    assert result.decision == "blocked"


def test_revision_required_needs_findings() -> None:
    with pytest.raises(ReviewParseError):
        parse_review_result('{"decision": "revision_required", "summary": "有问题"}')


def test_approved_with_high_severity_finding_is_rejected() -> None:
    with pytest.raises(ReviewParseError):
        parse_review_result(
            '{"decision": "approved", "findings": ['
            '{"severity": "high", "problem": "严重问题"}]}'
        )
