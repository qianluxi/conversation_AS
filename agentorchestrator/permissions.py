from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

DEFAULT_PERMISSION_TIMEOUT = 120.0


def select_option(options: list[dict[str, Any]],
                  kinds: tuple[str, ...] = ("allow_once", "allow_always")) -> dict[str, Any]:
    """按优先级选择许可选项；没有匹配选项时返回 cancelled。"""
    for kind in kinds:
        for option in options:
            if option.get("kind") == kind and option.get("optionId"):
                return {
                    "outcome": {"outcome": "selected", "optionId": option["optionId"]},
                }
    return {"outcome": {"outcome": "cancelled"}}


def cancelled_result() -> dict[str, Any]:
    return {"outcome": {"outcome": "cancelled"}}


@dataclass
class PendingPermission:
    request_id: str
    run_id: str
    agent: str
    tool: str
    options: list[dict[str, Any]]
    message: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    created_at: float = field(default_factory=time.time)
    resolved: bool = False


class PermissionBroker:
    """把 ACP 的 ask 请求转成 UI 可确认的挂起项，超时默认拒绝。"""

    def __init__(self, timeout: float = DEFAULT_PERMISSION_TIMEOUT) -> None:
        self.timeout = timeout
        self._pending: dict[str, PendingPermission] = {}

    async def request(self, run_id: str, agent: str,
                      message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or params.get("tool_call") or {}
        tool = " ".join(str(item) for item in (
            params.get("title"), params.get("toolName"),
            tool_call.get("title"), tool_call.get("name"), tool_call.get("kind"),
        ) if item) or "unknown"
        options = params.get("options") or []
        request = PendingPermission(
            request_id=f"perm_{uuid4().hex}",
            run_id=run_id,
            agent=agent,
            tool=tool,
            options=options,
            message=message,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request.request_id] = request
        try:
            return await asyncio.wait_for(
                asyncio.shield(request.future), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return cancelled_result()
        finally:
            self._pending.pop(request.request_id, None)

    def resolve(self, request_id: str, *, option_id: str | None = None,
                decision: str | None = None) -> dict[str, Any]:
        request = self._pending.get(request_id)
        if request is None:
            raise KeyError(f"未知的权限请求：{request_id}")
        if request.future.done():
            raise RuntimeError("该权限请求已经处理")
        if option_id:
            allowed = {str(o.get("optionId")) for o in request.options}
            if option_id not in allowed:
                raise ValueError(f"无效的权限选项：{option_id}")
            result = {
                "outcome": {"outcome": "selected", "optionId": option_id},
            }
        elif decision == "deny":
            result = cancelled_result()
        elif decision == "allow":
            result = select_option(request.options)
        else:
            raise ValueError("必须提供 option_id 或 decision=allow/deny")
        request.resolved = True
        request.future.set_result(result)
        return result

    def cancel_run(self, run_id: str) -> None:
        for request in list(self._pending.values()):
            if request.run_id == run_id and not request.future.done():
                request.resolved = True
                request.future.set_result(cancelled_result())

    def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        items = []
        for request in self._pending.values():
            if run_id is not None and request.run_id != run_id:
                continue
            items.append({
                "request_id": request.request_id,
                "run_id": request.run_id,
                "agent": request.agent,
                "tool": request.tool,
                "options": [
                    {
                        "option_id": option.get("optionId"),
                        "name": option.get("name"),
                        "kind": option.get("kind"),
                    }
                    for option in request.options
                ],
                "created_at": request.created_at,
            })
        return items
