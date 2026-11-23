from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..adapters import build_adapters
from ..adapters.base import AdapterError
from ..config import AppConfig
from ..orchestrator.engine import Conversation, run_collaboration
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


class ResumeRequest(BaseModel):
    timeout: float | None = Field(default=None, gt=0)
    resend_unstarted: bool = False


class ReleaseRequest(BaseModel):
    message_id: str
    force: bool = False
    timeout: float | None = Field(default=None, gt=0)


@dataclass
class RunHandle:
    run_id: str
    task: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


def _is_active(handle: RunHandle | None) -> bool:
    return bool(handle and handle.task and not handle.task.done())


def _json(model: Any) -> Any:
    if model is None:
        return None
    return model.model_dump(mode="json")


def _apply_opencode_model(adapters: dict[str, Any], spec: str | None) -> None:
    if not spec or "/" not in spec:
        return
    provider_id, model_id = spec.split("/", 1)
    if not provider_id or not model_id:
        return
    for adapter in adapters.values():
        if getattr(adapter, "kind", "") == "opencode":
            adapter.settings = adapter.settings.model_copy(update={
                "provider_id": provider_id,
                "model_id": model_id,
            })


def _run_payload(store: Store, handle: RunHandle | None, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    messages = store.list_messages(run_id)
    last_error = next((message.error for message in reversed(messages) if message.error), None)
    if not last_error:
        last_error = run.metadata.get("error") or run.metadata.get("reason")
    return {
        "run": _json(run),
        "steps": [_json(step) for step in store.list_steps(run_id)],
        "messages": [_json(message) for message in messages],
        "events": [_json(event) for event in store.list_events(run_id, limit=150)],
        "active": _is_active(handle),
        "error": handle.error if handle else None,
        "last_error": last_error,
        "result": handle.result if handle else None,
    }


def create_app(config: AppConfig, store: Store) -> FastAPI:
    app = FastAPI(title="conversation_AS", docs_url="/api/docs", redoc_url=None)
    app.state.config = config
    app.state.store = store
    app.state.handles = {}

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
            return {"agent": name, **data}
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

        adapters = build_adapters(cfg)
        _apply_opencode_model(adapters, payload.opencode_model)
        try:
            conversation = Conversation(store, adapters, goal=payload.goal)
        except Exception:
            for adapter in adapters.values():
                await adapter.aclose()
            raise

        handle = RunHandle(run_id=conversation.run_id)
        request.app.state.handles[handle.run_id] = handle

        async def runner() -> None:
            try:
                handle.result = await run_collaboration(
                    conversation,
                    planner=payload.planner, worker=payload.worker,
                    reviewer=payload.reviewer, max_rounds=payload.max_rounds,
                    goal=payload.goal, timeout=payload.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 记录到 UI
                handle.error = f"{type(exc).__name__}: {exc}"
                log.exception("run %s failed", handle.run_id)
            finally:
                await conversation.close()

        handle.task = asyncio.create_task(runner(), name=f"run-{handle.run_id}")
        return {"run_id": handle.run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        handle = request.app.state.handles.get(run_id)
        try:
            return _run_payload(request.app.state.store, handle, run_id)
        except KeyError:
            raise HTTPException(404, f"未知 run_id: {run_id}") from None

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
