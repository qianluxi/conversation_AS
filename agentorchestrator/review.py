from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


class ReviewParseError(ValueError):
    """审查结果不是合法结构。协调器不得据此自动批准。"""


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Finding(BaseModel):
    severity: Severity = Severity.MEDIUM
    file: str | None = None
    location: str | None = None
    problem: str
    suggested_action: str | None = None


class ReviewResult(BaseModel):
    decision: Literal["approved", "revision_required", "blocked"]
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _self_consistent(self) -> "ReviewResult":
        if self.decision == "revision_required" and not self.findings:
            raise ValueError("revision_required 必须至少包含一条 findings")
        if self.decision == "approved":
            severe = [f for f in self.findings
                      if f.severity in (Severity.HIGH, Severity.CRITICAL)]
            if severe:
                raise ValueError("存在 high/critical finding 时不能 approved")
        return self

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


REVIEW_CONTRACT = """只输出一个 JSON 对象，不要输出 JSON 之外的任何文字。格式：
{
  "decision": "approved" | "revision_required" | "blocked",
  "summary": "一句话结论",
  "findings": [
    {
      "severity": "info" | "low" | "medium" | "high" | "critical",
      "file": "相对路径或 null",
      "location": "行号/函数名或 null",
      "problem": "问题描述",
      "suggested_action": "建议修改方式或 null"
    }
  ],
  "evidence": ["可复核的证据，例如命令、文件片段、测试结果"],
  "confidence": 0.0
}
规则：只有证据充分且确认满足要求时才允许 approved；存在必须修改的问题时用
revision_required；无法完成审查或缺少关键信息时用 blocked。findings 必须如实列出。"""

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ReviewParseError("审查回复中没有可解析的 JSON 对象")


def parse_review_result(text: str) -> ReviewResult:
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        raise ReviewParseError("审查结果必须是 JSON 对象")
    if isinstance(payload.get("review"), dict):
        payload = payload["review"]
    try:
        return ReviewResult.model_validate(payload)
    except ValidationError as exc:
        raise ReviewParseError(f"审查结果字段不合法：{exc.errors()}") from exc


def review_as_prompt(result: ReviewResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
