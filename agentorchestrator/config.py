from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, Field


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
    timeout: float = 300.0
    poll_interval: float = 1.0
    verify_ssl: bool = True


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


class MockSettings(BaseModel):
    type: Literal["mock"] = "mock"
    role: Literal["planner", "worker", "reviewer", "echo"] = "echo"
    delay: float = 0.0


AgentSettings = Annotated[
    Union[OpencodeSettings, WorkBuddyLocalSettings, WorkBuddyAcpSettings, MockSettings],
    Field(discriminator="type"),
]


class StorageSettings(BaseModel):
    path: str = "data/orchestrator.db"


class AppConfig(BaseModel):
    agents: dict[str, AgentSettings] = Field(default_factory=dict)
    storage: StorageSettings = Field(default_factory=StorageSettings)
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
