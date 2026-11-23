from __future__ import annotations

from ..config import (
    AgentSettings,
    AppConfig,
    MockSettings,
    OpencodeSettings,
    WorkBuddyAcpSettings,
    WorkBuddyIpcSettings,
    WorkBuddyLocalSettings,
)
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRemoteError,
    AdapterTimeout,
    AdapterUncertain,
    AgentAdapter,
    AgentReply,
    HealthStatus,
    MessageRef,
)
from .mock import MockAdapter
from .opencode import OpenCodeAdapter
from .workbuddy_acp import WorkBuddyAcpAdapter
from .workbuddy_ipc import WorkBuddyIpcAdapter
from .workbuddy_local import LocalAssistantGate, WorkBuddyLocalAdapter

__all__ = [
    "AdapterAuthError",
    "AdapterError",
    "AdapterRemoteError",
    "AdapterTimeout",
    "AdapterUncertain",
    "AgentAdapter",
    "AgentReply",
    "HealthStatus",
    "MessageRef",
    "OpenCodeAdapter",
    "WorkBuddyLocalAdapter",
    "LocalAssistantGate",
    "WorkBuddyAcpAdapter",
    "WorkBuddyIpcAdapter",
    "MockAdapter",
    "build_adapter",
    "build_adapters",
]


def build_adapter(name: str, settings: AgentSettings,
                  *,
                  local_gate: LocalAssistantGate | None = None) -> AgentAdapter:
    if isinstance(settings, OpencodeSettings):
        return OpenCodeAdapter(name, settings)
    if isinstance(settings, WorkBuddyLocalSettings):
        return WorkBuddyLocalAdapter(name, settings, gate=local_gate)
    if isinstance(settings, WorkBuddyAcpSettings):
        return WorkBuddyAcpAdapter(name, settings)
    if isinstance(settings, WorkBuddyIpcSettings):
        return WorkBuddyIpcAdapter(name, settings)
    if isinstance(settings, MockSettings):
        return MockAdapter(name, settings)
    raise TypeError(f"未知的适配器类型：{type(settings).__name__}")


def build_adapters(config: AppConfig) -> dict[str, AgentAdapter]:
    # 同一 PC 本地助理（相同 base_url）的所有实例共享同一个远端状态门。
    local_gates: dict[str, LocalAssistantGate] = {}
    adapters: dict[str, AgentAdapter] = {}
    for name, settings in config.agents.items():
        gate = None
        if isinstance(settings, WorkBuddyLocalSettings):
            gate = local_gates.setdefault(settings.base_url, LocalAssistantGate())
        adapters[name] = build_adapter(name, settings, local_gate=gate)
    return adapters
