from __future__ import annotations

from ..config import (
    AgentSettings,
    AppConfig,
    MockSettings,
    OpencodeSettings,
    WorkBuddyAcpSettings,
    WorkBuddyLocalSettings,
)
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRemoteError,
    AdapterTimeout,
    AgentAdapter,
    AgentReply,
    HealthStatus,
    MessageRef,
)
from .mock import MockAdapter
from .opencode import OpenCodeAdapter
from .workbuddy_acp import WorkBuddyAcpAdapter
from .workbuddy_local import WorkBuddyLocalAdapter

__all__ = [
    "AdapterAuthError",
    "AdapterError",
    "AdapterRemoteError",
    "AdapterTimeout",
    "AgentAdapter",
    "AgentReply",
    "HealthStatus",
    "MessageRef",
    "OpenCodeAdapter",
    "WorkBuddyLocalAdapter",
    "WorkBuddyAcpAdapter",
    "MockAdapter",
    "build_adapter",
    "build_adapters",
]


def build_adapter(name: str, settings: AgentSettings) -> AgentAdapter:
    if isinstance(settings, OpencodeSettings):
        return OpenCodeAdapter(name, settings)
    if isinstance(settings, WorkBuddyLocalSettings):
        return WorkBuddyLocalAdapter(name, settings)
    if isinstance(settings, WorkBuddyAcpSettings):
        return WorkBuddyAcpAdapter(name, settings)
    if isinstance(settings, MockSettings):
        return MockAdapter(name, settings)
    raise TypeError(f"未知的适配器类型：{type(settings).__name__}")


def build_adapters(config: AppConfig) -> dict[str, AgentAdapter]:
    return {name: build_adapter(name, settings) for name, settings in config.agents.items()}
