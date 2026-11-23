from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentorchestrator.storage.database import Store


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    database = Store(tmp_path / "orchestrator.db")
    yield database
    database.close()


class FakeAuth:
    """测试用 OAuth 替身，避免读取真实 token 文件。"""

    def __init__(self) -> None:
        self.refreshed = 0

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"

    async def refresh(self) -> dict[str, Any]:
        self.refreshed += 1
        return {"access_token": "refreshed-token"}

    async def aclose(self) -> None:
        return None
