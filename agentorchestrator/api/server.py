from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..adapters import build_adapters
from ..adapters.base import AdapterError, AdapterTimeout
from ..config import AppConfig
from ..orchestrator.engine import Conversation, run_collaboration
from ..messaging.schema import RunPhase, RunStatus
from ..permissions import PermissionBroker
from ..storage.database import Store

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=1)
    planner: str
    worker: str
    reviewer: str | None = None
    max_rounds: int = Field(default=2, ge=1, le=10)
    timeout: float | None = Field(default=None, gt=0)
    opencode_model: str | None = None
    opencode_directory: str | None = None
    opencode_agent: str | None = None


class ResumeRequest(BaseModel):
    timeout: float | None = Field(default=None, gt=0)
    resend_unstarted: bool = False


class ReleaseRequest(BaseModel):
    message_id: str
    force: bool = False
    timeout: float | None = Field(default=None, gt=0)


class WaitRequest(BaseModel):
    message_id: str | None = None
    timeout: float = Field(default=600.0, gt=0, le=7200)


class ContinueRequest(BaseModel):
    retry_unknown: bool = False
    instruction: str | None = None


class PermissionResolveRequest(BaseModel):
    option_id: str | None = None
    decision: Literal["allow", "deny"] | None = None


@dataclass
class RunHandle:
    run_id: str
    task: asyncio.Task[None] | None = None
    conversation: Conversation | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


def _is_active(handle: RunHandle | None) -> bool:
    return bool(handle and handle.task and not handle.task.done())


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _basic_password(header: str) -> str | None:
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    _user, sep, password = raw.partition(":")
    return password if sep else None


def _json(model: Any) -> Any:
    if model is None:
        return None
    return model.model_dump(mode="json")


def _apply_opencode_overrides(adapters: dict[str, Any], spec: str | None,
                              directory: str | None,
                              agent: str | None = None) -> None:
    updates: dict[str, Any] = {}
    if spec and "/" in spec:
        provider_id, model_id = spec.split("/", 1)
        if provider_id and model_id:
            updates.update(provider_id=provider_id, model_id=model_id)
    if directory:
        updates["directory"] = directory
    if agent:
        updates["agent"] = agent
    if not updates:
        return
    for adapter in adapters.values():
        if getattr(adapter, "kind", "") == "opencode":
            adapter.settings = adapter.settings.model_copy(update=updates)


def _derive_workflow(store: Store, run: Any) -> dict[str, Any]:
    """为没有 workflow 元数据的旧 run 推导检查点。"""
    messages = store.list_messages(run.run_id)
    metadata = run.metadata or {}
    planner = metadata.get("planner") or "planner"
    worker = metadata.get("worker") or "worker"
    reviewer = metadata.get("reviewer") or planner
    plan = ""
    work = ""
    round_no = 0
    for message in messages:
        if (message.from_agent == planner and message.reply_to
                and message.turn_id == 0 and not plan):
            plan = message.content
        if message.from_agent == worker and message.reply_to:
            work = message.content
            round_no = max(round_no, message.turn_id)
    if plan:
        round_no = max(1, round_no)
    stage = "plan" if not plan else ("review" if work else "work")
    if run.status.value == "completed":
        stage = "done"
    return {
        "goal": run.goal, "planner": planner, "worker": worker, "reviewer": reviewer,
        "max_rounds": int(metadata.get("max_rounds") or 2),
        "workspace": metadata.get("workspace") or metadata.get("opencode_directory"),
        "stage": stage, "round": round_no, "plan": plan, "work": work,
        "issues": "", "notes": "", "review": "", "review_result": None,
        "derived": True,
    }


def _attach_permission_handlers(adapters: dict[str, Any], run_id: str,
                                broker: PermissionBroker) -> None:
    for name, adapter in adapters.items():
        if not hasattr(adapter, "permission_handler"):
            continue

        async def handler(message: dict[str, Any], _name: str = name) -> dict[str, Any]:
            return await broker.request(run_id, _name, message)

        adapter.permission_handler = handler


def _run_payload(store: Store, handle: RunHandle | None, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    messages = store.list_messages(run_id)
    last_error = next((message.error for message in reversed(messages) if message.error), None)
    if not last_error:
        last_error = run.metadata.get("error") or run.metadata.get("reason")
    worker_name = run.metadata.get("worker")
    worker_replies = [m for m in messages if worker_name and m.from_agent == worker_name]
    final_output = worker_replies[-1].content if worker_replies else ""
    workflow = run.metadata.get("workflow") or _derive_workflow(store, run)
    return {
        "run": _json(run),
        "workflow": workflow,
        "steps": [_json(step) for step in store.list_steps(run_id)],
        "messages": [_json(message) for message in messages],
        "events": [_json(event) for event in store.list_events(run_id, limit=150)],
        "active": _is_active(handle),
        "error": handle.error if handle else None,
        "last_error": last_error,
        "final_output": final_output,
        "result": handle.result if handle else None,
    }


def create_app(config: AppConfig, store: Store,
               auth_token: str | None = None) -> FastAPI:
    app = FastAPI(title="conversation_AS", docs_url="/api/docs", redoc_url=None)
    app.state.config = config
    app.state.store = store
    app.state.handles = {}
    app.state.permission_broker = PermissionBroker()
    app.state.auth_token = auth_token

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        token = request.app.state.auth_token
        if not token:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        supplied = ""
        if header.startswith("Basic "):
            supplied = _basic_password(header) or ""
        elif header.startswith("Bearer "):
            supplied = header[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, token):
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="conversation_AS"'},
            )
        return await call_next(request)

    # ---- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ---- discovery / health ----------------------------------------------

    @app.get("/api/agents")
    async def agents(request: Request) -> list[dict[str, str]]:
        cfg: AppConfig = request.app.state.config
        return [{"name": name, "type": settings.type}
                for name, settings in cfg.agents.items()]

    @app.get("/api/health")
    async def health(request: Request) -> list[dict[str, Any]]:
        cfg: AppConfig = request.app.state.config
        adapters = build_adapters(cfg)
        results: list[dict[str, Any]] = []
        try:
            for name, adapter in adapters.items():
                try:
                    status = await asyncio.wait_for(adapter.health_check(), timeout=25)
                    results.append({"name": name, "type": adapter.kind,
                                    "ok": status.ok, "detail": status.detail})
                except Exception as exc:  # noqa: BLE001 - 单个 agent 失败不影响其他
                    results.append({"name": name, "type": adapter.kind,
                                    "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            for adapter in adapters.values():
                try:
                    await adapter.aclose()
                except Exception:  # noqa: BLE001
                    pass
        return results

    @app.get("/api/opencode/models")
    async def opencode_models(request: Request, agent: str | None = None) -> dict[str, Any]:
        cfg: AppConfig = request.app.state.config
        adapters = build_adapters(cfg)
        try:
            candidates = [
                (name, adapter) for name, adapter in adapters.items()
                if getattr(adapter, "kind", "") == "opencode"
                and (agent is None or name == agent)
            ]
            if not candidates:
                raise HTTPException(400, "配置中没有可用的 opencode agent")
            name, adapter = candidates[0]
            try:
                data = await adapter.list_models()
            except AdapterError as exc:
                raise HTTPException(409, str(exc)) from exc
            try:
                path_info = await adapter.get_path()
            except Exception:  # noqa: BLE001 - 仅用于展示默认目录
                path_info = {}
            return {
                "agent": name,
                **data,
                "suggested_directory": adapter.settings.directory or os.getcwd(),
                "server_directory": path_info.get("directory"),
            }
        finally:
            for adapter in adapters.values():
                try:
                    await adapter.aclose()
                except Exception:  # noqa: BLE001
                    pass

    # ---- runs -------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs(request: Request, limit: int = 20) -> list[dict[str, Any]]:
        cfg: AppConfig = request.app.state.config
        _ = cfg
        return [_json(run) for run in request.app.state.store.list_runs(limit)]

    @app.post("/api/runs")
    async def start_run(payload: StartRunRequest, request: Request) -> dict[str, str]:
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        names = {payload.planner, payload.worker, payload.reviewer or payload.planner}
        missing = [name for name in names if name not in cfg.agents]
        if missing:
            raise HTTPException(400, f"未配置 agent: {', '.join(missing)}")

        directory = None
        if payload.opencode_directory:
            candidate = Path(payload.opencode_directory).expanduser()
            if not candidate.is_dir():
                raise HTTPException(400, f"OpenCode 工作目录不存在：{payload.opencode_directory}")
            directory = str(candidate.resolve())

        adapters = build_adapters(cfg)
        _apply_opencode_overrides(adapters, payload.opencode_model, directory,
                                  payload.opencode_agent)
        try:
            conversation = Conversation(store, adapters, goal=payload.goal)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        _attach_permission_handlers(
            adapters, conversation.run_id, request.app.state.permission_broker,
        )

        handle = RunHandle(run_id=conversation.run_id, conversation=conversation)
        request.app.state.handles[handle.run_id] = handle
        store.update_run(conversation.run_id, metadata={
            **conversation.run.metadata,
            "workspace": directory,
            "opencode_model": payload.opencode_model,
            "opencode_agent": payload.opencode_agent,
            "max_rounds": payload.max_rounds,
            "timeout": payload.timeout,
            "read_only": payload.opencode_agent == "plan",
        })
        conversation._refresh()

        async def runner() -> None:
            try:
                handle.result = await run_collaboration(
                    conversation,
                    planner=payload.planner, worker=payload.worker,
                    reviewer=payload.reviewer, max_rounds=payload.max_rounds,
                    goal=payload.goal, timeout=payload.timeout,
                    workspace=directory,
                    read_only=payload.opencode_agent == "plan",
                    verification_excludes=cfg.verification.exclude_dirs,
                    verification_limits=cfg.verification.model_dump(exclude={"exclude_dirs"}),
                )
            except Exception as exc:  # noqa: BLE001 - 记录到 UI
                handle.error = f"{type(exc).__name__}: {exc}"
                log.exception("run %s failed", handle.run_id)
            finally:
                await conversation.close()

        handle.task = asyncio.create_task(runner(), name=f"run-{handle.run_id}")
        return {"run_id": handle.run_id}

    @app.post("/api/runs/{run_id}/continue")
    async def continue_run(run_id: str, payload: ContinueRequest | None,
                           request: Request) -> dict[str, str]:
        _ensure_not_active(request, run_id)
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        parent = store.get_run(run_id)
        if parent is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        workflow = dict((parent.metadata or {}).get("workflow")
                        or _derive_workflow(store, parent))
        stage = workflow.get("stage")
        if stage not in ("plan", "work", "review"):
            raise HTTPException(409, f"没有可继续的检查点（stage={stage or 'unknown'}）")
        options = payload or ContinueRequest()
        if stage == "work" and not workflow.get("work") and not options.retry_unknown:
            raise HTTPException(
                409,
                "上次执行结果未知。确认要重试当前执行节点后，使用 retry_unknown=true。",
            )
        if stage == "work" and workflow.get("work"):
            stage = "review"

        goal = parent.goal
        if options.instruction:
            goal = f"{goal}\n【继续说明】{options.instruction}"
        workspace = (parent.metadata.get("workspace")
                     or parent.metadata.get("opencode_directory")
                     or workflow.get("workspace"))
        planner = workflow.get("planner") or parent.metadata.get("planner")
        worker = workflow.get("worker") or parent.metadata.get("worker")
        reviewer = workflow.get("reviewer") or parent.metadata.get("reviewer")
        if not planner or not worker:
            raise HTTPException(409, "父 run 缺少 planner/worker 信息，无法继续")
        adapters = build_adapters(cfg)
        model_spec = parent.metadata.get("opencode_model")
        agent_name = parent.metadata.get("opencode_agent")
        if not model_spec or not agent_name:
            # 旧 run 没有保存模型/角色：从它的 OpenCode 会话推断
            opencode_adapter = next(
                (adapter for adapter in adapters.values()
                 if getattr(adapter, "kind", "") == "opencode"),
                None,
            )
            worker_name = parent.metadata.get("worker") or "opencode"
            binding = store.get_binding(parent.run_id, worker_name)
            if opencode_adapter is not None and binding is not None:
                try:
                    session = await opencode_adapter.get_session(binding.provider_session_id)
                    agent_name = agent_name or session.get("agent")
                    model = session.get("model") or {}
                    if not model_spec and model.get("providerID") and model.get("id"):
                        model_spec = f"{model['providerID']}/{model['id']}"
                except Exception as exc:  # noqa: BLE001 - 推断失败则用配置默认
                    log.warning("无法从父 run 会话推断模型/角色：%s", exc)
        _apply_opencode_overrides(adapters, model_spec, workspace, agent_name)
        try:
            conversation = Conversation(store, adapters, goal=goal)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        _attach_permission_handlers(
            adapters, conversation.run_id, request.app.state.permission_broker,
        )

        new_workflow = dict(workflow)
        new_workflow.update({
            "parent_run_id": run_id,
            "goal": goal,
            "workspace": workspace,
            "stage": stage,
        })
        if options.instruction:
            new_workflow["notes"] = options.instruction
        elif stage == "work" and not new_workflow.get("work"):
            new_workflow["notes"] = (
                "继续上次未完成的工作；先检查工作目录中已有产物，避免重复创建或覆盖。"
            )
        max_rounds = int(parent.metadata.get("max_rounds")
                         or workflow.get("max_rounds") or 2)
        read_only = bool(parent.metadata.get("read_only")
                         or workflow.get("read_only")
                         or agent_name == "plan")
        metadata = {
            **conversation.run.metadata,
            "parent_run_id": run_id,
            "workspace": workspace,
            "opencode_model": model_spec,
            "opencode_agent": agent_name,
            "max_rounds": max_rounds,
            "timeout": parent.metadata.get("timeout"),
            "read_only": read_only,
            "workflow": new_workflow,
        }
        store.update_run(conversation.run_id, metadata=metadata)
        conversation._refresh()

        handle = RunHandle(run_id=conversation.run_id, conversation=conversation)
        request.app.state.handles[handle.run_id] = handle
        timeout = parent.metadata.get("timeout")

        async def runner() -> None:
            try:
                handle.result = await run_collaboration(
                    conversation,
                    planner=planner, worker=worker, reviewer=reviewer,
                    max_rounds=max_rounds, goal=goal, timeout=timeout,
                    workspace=workspace, read_only=read_only,
                    verification_excludes=cfg.verification.exclude_dirs,
                    verification_limits=cfg.verification.model_dump(exclude={"exclude_dirs"}),
                )
            except Exception as exc:  # noqa: BLE001
                handle.error = f"{type(exc).__name__}: {exc}"
                log.exception("continued run %s failed", handle.run_id)
            finally:
                await conversation.close()

        handle.task = asyncio.create_task(runner(), name=f"run-{handle.run_id}")
        return {"run_id": handle.run_id, "parent_run_id": run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        handle = request.app.state.handles.get(run_id)
        try:
            payload = _run_payload(request.app.state.store, handle, run_id)
        except KeyError:
            raise HTTPException(404, f"未知 run_id: {run_id}") from None
        payload["permissions"] = request.app.state.permission_broker.pending(run_id)
        return payload

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        handle: RunHandle | None = request.app.state.handles.get(run_id)
        if handle is not None and _is_active(handle):
            if handle.conversation is not None:
                try:
                    await asyncio.wait_for(handle.conversation.cancel_inflight(), timeout=5)
                except Exception:  # noqa: BLE001 - 尽力中止远端
                    pass
            if handle.task is not None:
                handle.task.cancel()
                try:
                    await handle.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            request.app.state.permission_broker.cancel_run(run_id)
            latest = store.get_run(run_id) or run
            return {"run_id": run_id, "status": latest.status.value,
                    "phase": latest.phase.value}
        if run.status is RunStatus.RUNNING:
            updated = store.transition_run(
                run_id, status=RunStatus.CANCELLED, phase=RunPhase.CANCELLED,
                event_type="run_cancelled", payload={"reason": "manual cancel"},
            )
            return {"run_id": run_id, "status": updated.status.value,
                    "phase": updated.phase.value}
        raise HTTPException(409, "该 run 当前没有执行中的任务")

    @app.get("/api/permissions")
    async def list_permissions(request: Request,
                               run_id: str | None = None) -> list[dict[str, Any]]:
        return request.app.state.permission_broker.pending(run_id)

    @app.post("/api/permissions/{request_id}")
    async def resolve_permission(request_id: str,
                                 payload: PermissionResolveRequest,
                                 request: Request) -> dict[str, Any]:
        broker: PermissionBroker = request.app.state.permission_broker
        try:
            result = broker.resolve(
                request_id, option_id=payload.option_id, decision=payload.decision,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": result, "permissions": broker.pending()}

    def _ensure_not_active(request: Request, run_id: str) -> None:
        if _is_active(request.app.state.handles.get(run_id)):
            raise HTTPException(409, "该 run 仍在执行，请等待完成后再操作")

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str, payload: ResumeRequest | None,
                         request: Request) -> dict[str, Any]:
        _ensure_not_active(request, run_id)
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        options = payload or ResumeRequest()
        adapters = build_adapters(cfg)
        try:
            conversation = Conversation(store, adapters, run_id=run_id)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        try:
            results = await conversation.resume(
                timeout=options.timeout,
                allow_resend_unstarted=options.resend_unstarted,
            )
        except AdapterError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await conversation.close()
        return {"results": results, "run": _json(store.get_run(run_id))}

    @app.post("/api/runs/{run_id}/wait")
    async def wait_run(run_id: str, payload: WaitRequest | None,
                       request: Request) -> dict[str, Any]:
        _ensure_not_active(request, run_id)
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        options = payload or WaitRequest()
        adapters = build_adapters(cfg)
        try:
            conversation = Conversation(store, adapters, run_id=run_id)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        try:
            message = await conversation.wait_unknown(
                options.message_id, timeout=options.timeout,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except AdapterTimeout as exc:
            raise HTTPException(408, str(exc)) from exc
        except (AdapterError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await conversation.close()
        return {"message": _json(message), "run": _json(store.get_run(run_id))}

    @app.post("/api/runs/{run_id}/abort")
    async def abort_run(run_id: str, request: Request) -> dict[str, Any]:
        _ensure_not_active(request, run_id)
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        adapters = build_adapters(cfg)
        try:
            conversation = Conversation(store, adapters, run_id=run_id)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        try:
            await conversation.abort_remote()
        finally:
            await conversation.close()
        return {"run_id": run_id, "run": _json(store.get_run(run_id))}

    @app.post("/api/runs/{run_id}/release")
    async def release_message(run_id: str, payload: ReleaseRequest,
                              request: Request) -> dict[str, Any]:
        _ensure_not_active(request, run_id)
        cfg: AppConfig = request.app.state.config
        store: Store = request.app.state.store
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"未知 run_id: {run_id}")
        adapters = build_adapters(cfg)
        try:
            conversation = Conversation(store, adapters, run_id=run_id)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise
        try:
            message = await conversation.release(
                payload.message_id, force=payload.force, timeout=payload.timeout,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (AdapterError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await conversation.close()
        return {"message": _json(message), "run": _json(store.get_run(run_id))}

    @app.get("/api/pending")
    async def pending(request: Request) -> list[dict[str, Any]]:
        store: Store = request.app.state.store
        return [_json(message) for message in store.list_unfinished()]

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error", exc_info=exc)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app
