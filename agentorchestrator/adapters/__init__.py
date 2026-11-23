from __future__ import annotations

import asyncio

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


def build_adapter(name: str, settings: AgentSettings,
                  *,
                  local_lock: asyncio.Lock | None = None) -> AgentAdapter:
    if isinstance(settings, OpencodeSettings):
        return OpenCodeAdapter(name, settings)
    if isinstance(settings, WorkBuddyLocalSettings):
        return WorkBuddyLocalAdapter(name, settings, lock=local_lock)
    if isinstance(settings, WorkBuddyAcpSettings):
        return WorkBuddyAcpAdapter(name, settings)
    if isinstance(settings, MockSettings):
        return MockAdapter(name, settings)
    raise TypeError(f"未知的适配器类型：{type(settings).__name__}")


def build_adapters(config: AppConfig) -> dict[str, AgentAdapter]:
    # 同一 PC 本地助理（相同 base_url）的所有实例共享一把锁，避免跨任务串扰。
    local_locks: dict[str, asyncio.Lock] = {}
    adapters: dict[str, AgentAdapter] = {}
    for name, settings in config.agents.items():
        lock = None
        if isinstance(settings, WorkBuddyLocalSettings):
            lock = local_locks.setdefault(settings.base_url, asyncio.Lock())
        adapters[name] = build_adapter(name, settings, local_lock=lock)
    return adapters
