from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, Field

from .verification import DEFAULT_EXCLUDE_DIRS


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """递归展开 ${VAR} / ${VAR:-default}。未定义且无默认值时保留原样。"""
    if isinstance(value, str):
        source = env if env is not None else os.environ

        def _replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in source:
                return source[name]
            return default if default is not None else match.group(0)

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, list):
        return [expand_env(item, env) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item, env) for key, item in value.items()}
    return value


_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")

_OPERATION_SYNONYMS: dict[str, set[str]] = {
    "execute": {
        "execute", "exec", "run", "command", "cmd", "shell", "bash", "powershell",
        "pwsh", "sh", "terminal", "spawn", "subprocess", "script",
    },
    "delete": {
        "delete", "remove", "rm", "del", "unlink", "destroy", "drop", "truncate",
        "purge", "erase", "format", "wipe",
    },
    "install": {
        "install", "uninstall", "reinstall", "pip", "npm", "yarn", "apt", "brew",
        "yum", "package", "dependency", "dependencies",
    },
    "write": {
        "write", "edit", "modify", "patch", "update", "create", "append", "save",
        "put", "set", "insert", "replace", "rename", "move", "copy",
    },
    "network": {
        "network", "http", "https", "url", "request", "fetch", "curl", "wget",
        "browse", "web", "download", "upload",
    },
    "test": {
        "test", "tests", "pytest", "jest", "unittest", "spec", "check", "lint",
        "ruff", "mypy",
    },
    "read": {
        "read", "view", "list", "search", "grep", "find", "glob", "cat", "open",
        "inspect", "analyze", "analyse", "scan", "stat", "query", "get", "lookup",
        "ls", "dir",
    },
}

_TOKEN_OPERATION: dict[str, str] = {}
for _operation, _words in _OPERATION_SYNONYMS.items():
    for _word in _words:
        _TOKEN_OPERATION.setdefault(_word, _operation)

_OPERATIONS = set(_OPERATION_SYNONYMS)

_DEFAULT_OPERATION_CATEGORY: dict[str, str] = {
    "read": "allow",
    "write": "ask",
    "test": "ask",
    "network": "ask",
    "execute": "deny",
    "delete": "deny",
    "install": "deny",
}

_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}


def tokenize_tool(tool: str) -> set[str]:
    """把工具名/标题拆成 token（含 camelCase），用于精确匹配而非子串匹配。"""
    if not tool:
        return set()
    spaced = _CAMEL_BOUNDARY.sub(r"\1 \2", str(tool))
    return {token.lower() for token in _TOKEN_SPLIT.split(spaced) if token}


class ToolCapability(BaseModel):
    name: str
    tokens: set[str] = Field(default_factory=set)
    operations: set[str] = Field(default_factory=set)
    category: Literal["allow", "ask", "deny"] = "ask"


class PermissionSettings(BaseModel):
    """基于工具能力（operation）的授权策略。

    - 只做 token 级匹配，避免 "spreadsheet.write" 因为包含 "read" 被放行；
    - 同一工具包含多种操作时取最严格类别（deny > ask > allow）；
    - 无法识别操作类型的工具一律 ask。
    """

    allow: list[str] = Field(default_factory=lambda: ["read"])
    ask: list[str] = Field(default_factory=lambda: ["write", "test", "network"])
    deny: list[str] = Field(
        default_factory=lambda: ["execute", "delete", "install"]
    )

    @staticmethod
    def _ops_for(value: str) -> set[str]:
        ops: set[str] = set()
        for token in tokenize_tool(value):
            if token in _OPERATIONS:
                ops.add(token)
            elif token in _TOKEN_OPERATION:
                ops.add(_TOKEN_OPERATION[token])
        return ops

    def _operation_categories(self) -> dict[str, str]:
        mapping = dict(_DEFAULT_OPERATION_CATEGORY)
        explicit = set(getattr(self, "model_fields_set", set())) & {"allow", "ask", "deny"}
        if not explicit:
            return mapping
        configured: dict[str, str] = {}
        # 显式配置内部同样按 allow -> ask -> deny 处理，冲突时更严格者胜出。
        for category in ("allow", "ask", "deny"):
            if category not in explicit:
                continue
            for item in getattr(self, category):
                for operation in self._ops_for(item):
                    configured[operation] = category
        mapping.update(configured)
        return mapping

    def classify(self, tool: str) -> ToolCapability:
        tokens = tokenize_tool(tool)
        operations = {
            _TOKEN_OPERATION[token] for token in tokens if token in _TOKEN_OPERATION
        }
        operations |= {token for token in tokens if token in _OPERATIONS}
        if not operations:
            return ToolCapability(name=tool, tokens=tokens, operations=set(),
                                  category="ask")
        mapping = self._operation_categories()
        categories = {mapping.get(operation, "ask") for operation in operations}
        category = max(categories, key=lambda item: _STRICTNESS[item])
        return ToolCapability(name=tool, tokens=tokens, operations=operations,
                              category=category)

    def category(self, tool: str) -> Literal["allow", "ask", "deny"]:
        return self.classify(tool).category



class OpencodeSettings(BaseModel):
    type: Literal["opencode"] = "opencode"
    base_url: str = "http://127.0.0.1:4096"
    username: str = "opencode"
    password: str = ""
    api_style: Literal["legacy", "v2"] = "legacy"
    delivery: Literal["async", "sync"] = "async"
    directory: str | None = None
    agent: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    timeout: float = 600.0
    inactivity_timeout: float = 300.0
    poll_interval: float = 1.0
    verify_ssl: bool = True
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)


class WorkBuddyLocalSettings(BaseModel):
    type: Literal["workbuddy_local"] = "workbuddy_local"
    base_url: str = "https://www.workbuddy.cn"
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scope: str = "user.localassistant.readable user.localassistant.invokable"
    token_file: str = "data/workbuddy_token.json"
    timeout: float = 300.0
    poll_interval: float = 2.0
    verify_ssl: bool = True
    lock_file: str = "data/workbuddy_local.lock"
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)


class WorkBuddyAcpSettings(BaseModel):
    type: Literal["workbuddy_acp"] = "workbuddy_acp"
    base_url: str = "https://www.workbuddy.cn"
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scope: str = "user.task.readable user.task.invokable"
    token_file: str = "data/workbuddy_token.json"
    bootstrap_prompt: str = "初始化协作会话，请等待后续指令。"
    cwd: str = "/workspace"
    timeout: float = 600.0
    poll_interval: float = 2.0
    auto_approve: bool = False
    verify_ssl: bool = True
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)


class WorkBuddyIpcSettings(BaseModel):
    """实验性：通过 WorkBuddy 桌面端本地 IPC（wbipc）调用内置 chat 接口。

    不需要 OAuth，但依赖 WorkBuddy 正在运行、已登录，且版本包含 wbipc。
    """

    type: Literal["workbuddy_ipc"] = "workbuddy_ipc"
    client_id: str = "conversation-as"
    client_kind: str = "skill"
    model: str = "auto"
    chat_path: str = "/v2/chat/completions"
    timeout: float = 300.0
    history_limit: int = 20
    library_path: str | None = None


class MockSettings(BaseModel):
    type: Literal["mock"] = "mock"
    role: Literal["planner", "worker", "reviewer", "echo"] = "echo"
    delay: float = 0.0


AgentSettings = Annotated[
    Union[
        OpencodeSettings,
        WorkBuddyLocalSettings,
        WorkBuddyAcpSettings,
        WorkBuddyIpcSettings,
        MockSettings,
    ],
    Field(discriminator="type"),
]


class StorageSettings(BaseModel):
    path: str = "data/orchestrator.db"


class VerificationSettings(BaseModel):
    exclude_dirs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    max_files: int = 50_000
    max_total_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    scan_timeout: float = 30.0


class AppConfig(BaseModel):
    agents: dict[str, AgentSettings] = Field(default_factory=dict)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    log_level: str = "INFO"

    def agent(self, name: str) -> AgentSettings:
        try:
            return self.agents[name]
        except KeyError:
            raise KeyError(f"配置中不存在 agent: {name}（可选：{', '.join(self.agents) or '无'}）") from None

    def workbuddy_settings(self, name: str | None = None) -> WorkBuddyLocalSettings | WorkBuddyAcpSettings:
        if name:
            settings = self.agent(name)
            if not isinstance(settings, (WorkBuddyLocalSettings, WorkBuddyAcpSettings)):
                raise TypeError(f"agent {name} 不是 WorkBuddy 适配器")
            return settings
        for settings in self.agents.values():
            if isinstance(settings, (WorkBuddyLocalSettings, WorkBuddyAcpSettings)):
                return settings
        raise KeyError("配置中没有 WorkBuddy agent")


def _candidate_paths(path: str | Path | None) -> list[Path]:
    if path:
        return [Path(path)]
    return [Path("config.yaml"), Path("config.example.yaml")]


def load_config(path: str | Path | None = None) -> AppConfig:
    candidates = _candidate_paths(path)
    for candidate in candidates:
        if candidate.exists():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            expanded = expand_env(raw)
            return AppConfig.model_validate(expanded)
    searched = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(f"未找到配置文件（已尝试：{searched}）")
