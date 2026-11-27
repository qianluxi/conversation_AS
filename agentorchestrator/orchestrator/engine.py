from __future__ import annotations

from typing import Any

from ..adapters.base import (
    AdapterError,
    AdapterTimeout,
    AdapterUncertain,
    AgentAdapter,
    AgentReply,
    MessageRef,
)
from ..messaging.schema import (
    TERMINAL_PHASES,
    Message,
    MessageStatus,
    Run,
    RunPhase,
    RunStatus,
    SessionBinding,
    Step,
    StepState,
    utc_now,
)
from ..review import REVIEW_CONTRACT, ReviewParseError, ReviewResult, parse_review_result, review_as_prompt
from ..storage.database import Store


class Conversation:
    """协调器核心：run 状态机、agent 会话映射、消息与步骤持久化。"""

    def __init__(self, store: Store, adapters: dict[str, AgentAdapter], *,
                 goal: str = "", run_id: str | None = None) -> None:
        self.store = store
        self.adapters = adapters
        if run_id is None:
            self.run = store.create_run(goal=goal)
            store.append_event(self.run.run_id, "run_created", RunPhase.CREATED,
                               payload={"goal": goal})
        else:
            existing = store.get_run(run_id)
            if existing is None:
                raise KeyError(f"未知 run_id: {run_id}")
            self.run = existing
            if existing.phase is not RunPhase.COMPLETED and existing.status is RunStatus.RUNNING:
                store.append_event(run_id, "run_loaded", existing.phase)
        self.turn = self.run.current_turn

    @property
    def run_id(self) -> str:
        return self.run.run_id

    # ---- state machine ----------------------------------------------------

    def _refresh(self) -> None:
        latest = self.store.get_run(self.run_id)
        if latest is not None:
            self.run = latest
            self.turn = latest.current_turn

    def event(self, event_type: str, *, step_id: str | None = None,
              payload: dict[str, Any] | None = None,
              phase: RunPhase | None = None) -> None:
        self.store.append_event(self.run_id, event_type, phase or self.run.phase,
                                step_id=step_id, payload=payload or {})

    def set_phase(self, phase: RunPhase, *, event_type: str = "phase_changed",
                  payload: dict[str, Any] | None = None) -> None:
        if self.run.phase is phase:
            return
        self.run = self.store.transition_run(
            self.run_id, phase=phase, current_turn=self.turn,
            event_type=event_type, payload=payload or {"phase": phase.value},
        )
        self.turn = self.run.current_turn

    def finish(self, status: RunStatus, phase: RunPhase,
               metadata: dict[str, Any] | None = None) -> None:
        merged = {**self.run.metadata, **(metadata or {})}
        merged.setdefault("finished_at", utc_now().isoformat())
        self.run = self.store.transition_run(
            self.run_id, status=status, phase=phase, current_turn=self.turn,
            metadata=merged, event_type=f"run_{status.value}", payload=metadata or {},
        )
        self.turn = self.run.current_turn

    # ---- agents -----------------------------------------------------------

    def adapter(self, name: str) -> AgentAdapter:
        try:
            return self.adapters[name]
        except KeyError:
            raise KeyError(
                f"未配置 agent: {name}（可选：{', '.join(self.adapters) or '无'}）"
            ) from None

    async def session_for(self, agent: str) -> SessionBinding:
        binding = self.store.get_binding(self.run_id, agent)
        if binding is not None:
            return binding
        adapter = self.adapter(agent)
        provider_session_id = await adapter.create_session(self.run_id)
        binding = SessionBinding(
            run_id=self.run_id, agent=agent, provider=adapter.kind,
            provider_session_id=provider_session_id,
        )
        self.store.upsert_binding(binding)
        self.event("session_created", payload={
            "agent": agent, "provider": adapter.kind,
            "provider_session_id": provider_session_id,
        })
        return binding

    # ---- messaging --------------------------------------------------------

    async def ask(self, agent: str, text: str, *, to_agent: str = "orchestrator",
                  turn_id: int | None = None, dedupe_key: str | None = None,
                  timeout: float | None = None, role: str = "worker",
                  phase: RunPhase | None = None) -> AgentReply:
        adapter = self.adapter(agent)

        if dedupe_key:
            existing = self.store.find_by_dedupe(self.run_id, dedupe_key)
            if existing is not None:
                reply = self.store.find_reply(existing.message_id)
                if reply is None and existing.status is MessageStatus.FAILED:
                    self._refresh()
                    if (self.run.status is not RunStatus.RUNNING
                            or self.run.phase in TERMINAL_PHASES):
                        raise RuntimeError(
                            "当前 run 已结束，不能重试 FAILED 消息；请新建 run。"
                        )
                    binding = await self.session_for(agent)
                    self.store.update_message(existing.message_id,
                                              status=MessageStatus.CREATED, error=None)
                    existing.status = MessageStatus.CREATED
                    step_id = (existing.metadata or {}).get("step_id")
                    step = self.store.get_step(step_id) if step_id else None
                    if step is None:
                        step = Step(run_id=self.run_id, turn_id=existing.turn_id,
                                    agent=agent, role=role)
                        self.store.create_step(step)
                    self.store.update_step(step.step_id, state=StepState.RUNNING,
                                           attempt=step.attempt + 1, error=None,
                                           finished_at=None)
                    self.event("message_retry", step_id=step.step_id,
                               payload={"message_id": existing.message_id})
                    return await self._dispatch(existing, binding, adapter, step, timeout)
                if reply is None:
                    raise RuntimeError(
                        f"幂等键 {dedupe_key} 命中的消息状态为 {existing.status.value}，"
                        "尚未得到回复。请先运行 `resume` 核对外部状态，不要重复提交。"
                    )
                return AgentReply(
                    session_id=existing.provider_session_id or "",
                    text=reply.content,
                    provider_message_id=reply.provider_message_id,
                    metadata={"deduplicated": True, "stored_message_id": reply.message_id,
                              **reply.metadata},
                )

        self._refresh()
        if self.run.status is not RunStatus.RUNNING or self.run.phase in TERMINAL_PHASES:
            raise RuntimeError(
                f"run {self.run_id} 已结束（status={self.run.status.value}, "
                f"phase={self.run.phase.value}），不能继续发送消息。"
            )
        if adapter.kind == "workbuddy_local":
            pending = self.store.list_unfinished(provider=adapter.kind)
            if pending:
                sample = ", ".join(
                    f"{item.run_id}/{item.message_id}={item.status.value}"
                    for item in pending[:3]
                )
                raise AdapterError(
                    "WorkBuddy 本地助理存在未确认的远端任务，已禁止新消息："
                    f"{sample}。请先运行 `resume` 核对，或 `release` 人工解除。"
                )

        if phase is not None:
            self.set_phase(phase)

        binding = await self.session_for(agent)
        turn = self.turn if turn_id is None else turn_id
        step = Step(run_id=self.run_id, turn_id=turn, agent=agent, role=role)
        self.store.create_step(step)
        outgoing = Message(
            run_id=self.run_id, turn_id=turn, from_agent="orchestrator", to_agent=agent,
            content=text, status=MessageStatus.CREATED, provider=adapter.kind,
            provider_session_id=binding.provider_session_id, dedupe_key=dedupe_key,
            metadata={"step_id": step.step_id, "role": role},
        )
        self.store.save_message(outgoing)
        self.store.update_step(step.step_id, input_message_id=outgoing.message_id)
        self.store.start_step(step.step_id)
        self.event("step_started", step_id=step.step_id, payload={
            "agent": agent, "turn_id": turn, "role": role,
        })
        return await self._dispatch(outgoing, binding, adapter, step, timeout)

    async def _dispatch(self, outgoing: Message, binding: SessionBinding,
                        adapter: AgentAdapter, step: Step | None,
                        timeout: float | None) -> AgentReply:
        attempt = outgoing.attempt + 1
        self.store.update_message(
            outgoing.message_id, status=MessageStatus.SENDING, attempt=attempt,
            last_attempt_at=utc_now(), error=None,
        )
        self.event("message_sending", step_id=step.step_id if step else None,
                   payload={"message_id": outgoing.message_id, "attempt": attempt})

        try:
            ref = await adapter.send_message(
                binding.provider_session_id, outgoing.content, timeout=timeout,
            )
        except (AdapterTimeout, AdapterUncertain) as exc:
            self._mark_unknown(outgoing, step, exc, phase="send_uncertain")
            raise
        except AdapterError as exc:
            self._mark_failed(outgoing, step, exc, phase="send_failed")
            raise

        metadata = {**outgoing.metadata, "ref": ref.metadata}
        self.store.update_message(
            outgoing.message_id, status=MessageStatus.ACKNOWLEDGED,
            provider_message_id=ref.message_id, metadata=metadata,
        )
        self.event("message_acknowledged", step_id=step.step_id if step else None,
                   payload={"message_id": outgoing.message_id,
                            "provider_message_id": ref.message_id})

        try:
            reply = await adapter.wait_response(ref, timeout=timeout)
        except AdapterTimeout as exc:
            self._mark_unknown(outgoing, step, exc, phase="wait_timeout")
            raise
        except AdapterError as exc:
            self._mark_unknown(outgoing, step, exc, phase="wait_error")
            raise

        return self._complete(outgoing, reply, binding, step)

    def _mark_unknown(self, outgoing: Message, step: Step | None,
                      exc: Exception, *, phase: str) -> None:
        self.store.update_message(
            outgoing.message_id, status=MessageStatus.UNKNOWN, error=str(exc),
        )
        if step is not None:
            self.store.finish_step(step.step_id, StepState.FAILED, error=str(exc))
            self.event("step_failed", step_id=step.step_id,
                       payload={"message_id": outgoing.message_id, "error_type": phase})
        self.event("message_unknown", step_id=step.step_id if step else None, payload={
            "message_id": outgoing.message_id, "error_type": phase, "error": str(exc),
        })

    def _mark_failed(self, outgoing: Message, step: Step | None,
                     exc: Exception, *, phase: str) -> None:
        self.store.update_message(
            outgoing.message_id, status=MessageStatus.FAILED, error=str(exc),
        )
        if step is not None:
            self.store.finish_step(step.step_id, StepState.FAILED, error=str(exc))
            self.event("step_failed", step_id=step.step_id,
                       payload={"message_id": outgoing.message_id, "error_type": phase})
        self.event("message_failed", step_id=step.step_id if step else None, payload={
            "message_id": outgoing.message_id, "error_type": phase, "error": str(exc),
        })

    def _complete(self, outgoing: Message, reply: AgentReply, binding: SessionBinding,
                  step: Step | None) -> AgentReply:
        reply_message = Message(
            run_id=self.run_id, turn_id=outgoing.turn_id, from_agent=outgoing.to_agent,
            to_agent=outgoing.from_agent, content=reply.text,
            status=MessageStatus.COMPLETED, provider=binding.provider,
            provider_session_id=binding.provider_session_id,
            provider_message_id=reply.provider_message_id,
            reply_to=outgoing.message_id,
            metadata={"stop_reason": reply.stop_reason, "usage": reply.usage},
        )
        next_turn = max(self.turn, outgoing.turn_id + 1)
        self.store.complete_exchange(
            outgoing_message_id=outgoing.message_id,
            reply=reply_message,
            step_id=step.step_id if step else None,
            run_id=self.run_id,
            current_turn=next_turn,
        )
        self.turn = next_turn
        self._refresh()
        return AgentReply(
            session_id=reply.session_id, text=reply.text,
            provider_message_id=reply.provider_message_id,
            stop_reason=reply.stop_reason, usage=reply.usage,
            metadata={
                **reply.metadata,
                "stored_message_id": reply_message.message_id,
                "step_id": step.step_id if step else None,
            },
        )

    def attach_reply_metadata(self, reply: AgentReply, data: dict[str, Any]) -> None:
        message_id = reply.metadata.get("stored_message_id")
        if not message_id:
            return
        stored = self.store.get_message(message_id)
        if stored is None:
            return
        self.store.update_message(message_id, metadata={**stored.metadata, **data})

    # ---- recovery ---------------------------------------------------------

    def get_workflow(self) -> dict[str, Any]:
        return dict(self.run.metadata.get("workflow") or {})

    def save_workflow(self, workflow: dict[str, Any],
                      *, event_type: str = "workflow_checkpoint") -> None:
        metadata = dict(self.run.metadata)
        metadata["workflow"] = workflow
        self.store.update_run(self.run_id, metadata=metadata)
        self._refresh()
        self.event(event_type, payload={
            "stage": workflow.get("stage"),
            "round": workflow.get("round"),
        })

    async def resume(self, *, timeout: float | None = None,
                     allow_resend_unstarted: bool = False) -> list[dict[str, Any]]:
        """核对未完成消息的外部状态；默认绝不重发可能已执行的消息。"""
        self._refresh()
        if self.run.status is not RunStatus.RUNNING or self.run.phase in TERMINAL_PHASES:
            return []
        unfinished = self.store.list_unfinished_messages(self.run_id)
        self._reconcile_steps()
        if not unfinished:
            return []
        self.set_phase(RunPhase.RECOVERING, event_type="run_recovering",
                       payload={"pending": len(unfinished)})
        results: list[dict[str, Any]] = []
        for message in unfinished:
            adapter = self.adapters.get(message.to_agent)
            binding = self.store.get_binding(self.run_id, message.to_agent)
            if adapter is None or binding is None:
                results.append({"message_id": message.message_id, "action": "skipped",
                                "reason": "adapter_or_session_missing"})
                continue

            step_id = (message.metadata or {}).get("step_id")
            step = self.store.get_step(step_id) if step_id else None

            if not message.provider_message_id:
                if allow_resend_unstarted and message.status in (
                    MessageStatus.CREATED, MessageStatus.PENDING,
                ) and message.attempt == 0:
                    try:
                        if step is not None and step.started_at is None:
                            self.store.start_step(step.step_id)
                        reply = await self._dispatch(message, binding, adapter, step, timeout)
                        results.append({"message_id": message.message_id, "action": "resent",
                                        "text": reply.text})
                    except AdapterError as exc:
                        results.append({"message_id": message.message_id, "action": "error",
                                        "reason": str(exc)})
                else:
                    results.append({"message_id": message.message_id, "action": "unresolved",
                                    "reason": "never_acknowledged"})
                continue

            ref = MessageRef(
                session_id=binding.provider_session_id,
                message_id=message.provider_message_id,
                metadata=(message.metadata or {}).get("ref") or {},
            )
            try:
                reply = await adapter.recover_reply(binding.provider_session_id, ref)
            except Exception as exc:  # noqa: BLE001 - 恢复是尽力而为
                results.append({"message_id": message.message_id, "action": "error",
                                "reason": str(exc)})
                continue
            if reply is None:
                results.append({"message_id": message.message_id, "action": "unresolved",
                                "reason": f"status={message.status.value}"})
                continue
            completed = self._complete(message, reply, binding, step)
            self.event("message_recovered", step_id=step_id, payload={
                "message_id": message.message_id, "text": completed.text[:200],
            })
            results.append({"message_id": message.message_id, "action": "recovered",
                            "text": completed.text})
        return results

    def transcript(self) -> list[Message]:
        return self.store.list_messages(self.run_id)

    async def release(self, message_id: str, *, force: bool = False,
                      timeout: float | None = None) -> Message:
        """人工释放不确定消息：先尽力核对远端，force 时才放弃等待。"""
        message = self.store.get_message(message_id)
        if message is None:
            raise KeyError(f"未知 message_id: {message_id}")
        if message.run_id != self.run_id:
            raise ValueError(f"消息 {message_id} 不属于 run {self.run_id}")
        if message.reply_to is not None:
            raise ValueError("回复消息不能被 release")
        if self.store.find_reply(message_id) is not None:
            raise RuntimeError("该消息已有回复，请使用 resume 完成收尾")
        if message.status in (MessageStatus.COMPLETED, MessageStatus.CANCELLED):
            return message

        adapter = self.adapters.get(message.to_agent)
        binding = self.store.get_binding(self.run_id, message.to_agent)
        if not force:
            if adapter is not None and binding is not None and message.provider_message_id:
                ref = MessageRef(
                    session_id=binding.provider_session_id,
                    message_id=message.provider_message_id,
                    metadata=(message.metadata or {}).get("ref") or {},
                )
                reply = await adapter.recover_reply(binding.provider_session_id, ref)
                if reply is not None:
                    step_id = (message.metadata or {}).get("step_id")
                    step = self.store.get_step(step_id) if step_id else None
                    self._complete(message, reply, binding, step)
                    completed = self.store.get_message(message_id)
                    assert completed is not None
                    return completed
            raise AdapterError(
                "无法确认远端任务是否结束。请先在外部系统确认，"
                "确需放弃时使用 --force 释放（可能丢失上一轮回复）。"
            )

        if adapter is not None:
            release_quarantine = getattr(adapter, "release_quarantine", None)
            if release_quarantine is not None:
                await release_quarantine(force=True,
                                         message_id=message.provider_message_id)
            elif binding is not None:
                try:
                    await adapter.remote_abort(binding.provider_session_id)
                except AdapterError:
                    await adapter.cancel_wait(binding.provider_session_id)
        released = self.store.release_message(
            self.run_id, message_id,
            reason="manual force release（已放弃等待远端结果）",
        )
        self.event("run_released", step_id=(message.metadata or {}).get("step_id"),
                   payload={"message_id": message_id})
        return released

    def _reconcile_steps(self) -> None:
        """修复崩溃窗口：消息已完成/失败，但 step 仍停留在 running。"""
        messages = self.store.list_messages(self.run_id)
        by_step: dict[str, list[Message]] = {}
        for message in messages:
            step_id = (message.metadata or {}).get("step_id")
            if step_id:
                by_step.setdefault(step_id, []).append(message)
        for step in self.store.list_steps(self.run_id):
            if step.state is not StepState.RUNNING:
                continue
            related = [m for m in by_step.get(step.step_id, [])
                       if m.from_agent == "orchestrator"]
            if not related:
                continue
            message = related[-1]
            if message.status is MessageStatus.COMPLETED:
                reply = self.store.find_reply(message.message_id)
                if reply is None:
                    self.store.finish_step(
                        step.step_id, StepState.FAILED,
                        error="reply_missing：消息标记完成但回复不存在（历史不一致）",
                    )
                    self.event("step_reconciled", step_id=step.step_id,
                               payload={"state": "failed", "reason": "reply_missing"})
                    continue
                self.store.finish_step(
                    step.step_id, StepState.COMPLETED,
                    output_message_id=reply.message_id,
                )
                self.event("step_reconciled", step_id=step.step_id,
                           payload={"state": "completed"})
            elif message.status in (MessageStatus.FAILED, MessageStatus.UNKNOWN):
                self.store.finish_step(step.step_id, StepState.FAILED, error=message.error)
                self.event("step_reconciled", step_id=step.step_id,
                           payload={"state": "failed"})

    async def close(self) -> None:
        seen: set[int] = set()
        for adapter in self.adapters.values():
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            await adapter.aclose()


async def run_collaboration(conversation: Conversation, *,
                            planner: str, worker: str, reviewer: str | None = None,
                            max_rounds: int = 2, goal: str = "",
                            timeout: float | None = None,
                            workspace: str | None = None) -> dict[str, Any]:
    """结构化流程：分析 -> 执行 -> 结构化审查；支持从持久化检查点继续。"""
    reviewer = reviewer or planner
    workflow = conversation.get_workflow()
    resuming = bool(workflow)
    if not workflow:
        workflow = {
            "goal": goal, "planner": planner, "worker": worker, "reviewer": reviewer,
            "max_rounds": max_rounds, "workspace": workspace,
            "stage": "plan", "round": 0, "plan": "", "work": "",
            "issues": "", "notes": "", "review": "", "review_result": None,
            "created_at": utc_now().isoformat(),
        }
    else:
        workflow.setdefault("stage", "plan")
        workflow.setdefault("round", 0)
        workflow.setdefault("plan", "")
        workflow.setdefault("work", "")
        workflow.setdefault("issues", "")
        workflow.setdefault("notes", "")
        workflow.setdefault("review", "")
        workflow.setdefault("review_result", None)
        workflow["goal"] = workflow.get("goal") or goal
        workflow["planner"] = workflow.get("planner") or planner
        workflow["worker"] = workflow.get("worker") or worker
        workflow["reviewer"] = workflow.get("reviewer") or reviewer
        workflow["max_rounds"] = int(workflow.get("max_rounds") or max_rounds)
        workflow["workspace"] = workflow.get("workspace") or workspace

    max_rounds = int(workflow.get("max_rounds") or max_rounds)
    workspace = workflow.get("workspace") or workspace
    stage = workflow.get("stage") or "plan"
    round_no = int(workflow.get("round") or 0)
    plan = str(workflow.get("plan") or "")
    work_text = str(workflow.get("work") or "")
    issues = str(workflow.get("issues") or "")

    # 规范化中断状态：工作已完成则进入审查；没有工作结果则回到执行。
    if stage == "plan" and plan:
        stage = "work"
        workflow["stage"] = "work"
    if stage == "work" and work_text:
        stage = "review"
        workflow["stage"] = "review"
    if stage == "review" and not work_text:
        stage = "work"
        workflow["stage"] = "work"
    if stage in ("done", "blocked", "failed", "max_rounds"):
        raise RuntimeError(f"workflow 已结束（stage={stage}），不能继续")
    if stage == "work" and round_no <= 0:
        round_no = 1
        workflow["round"] = 1

    metadata: dict[str, Any] = {
        "planner": planner, "worker": worker, "reviewer": reviewer,
        "workspace": workspace,
    }
    workspace_context = ""
    if workspace:
        workspace_context = (
            f"当前 OpenCode 工作目录：{workspace}\n"
            "代码就在该目录；用户说“检查代码/审查项目”时，"
            "不要要求用户粘贴代码或等待补充，直接在该目录检查。\n"
        )
    conversation.save_workflow(
        workflow, event_type="workflow_resumed" if resuming else "workflow_created",
    )

    review: ReviewResult | None = None
    review_reply: AgentReply | None = None
    work_reply: AgentReply | None = None

    try:
        if stage == "plan":
            conversation.set_phase(RunPhase.ANALYZING)
            plan_reply = await conversation.ask(
                planner,
                "你是需求分析与方案设计者。请针对下面的协作任务形成可执行的任务方案。\n"
                f"任务：{workflow.get('goal') or goal}\n"
                f"{workspace_context}"
                "要求：\n"
                "- 你无法访问文件系统或执行命令，不要猜测仓库内容，也不要假设 README/代码现状；\n"
                "- 但执行者可以访问上述工作目录；检查代码任务应在方案中明确要求执行者在"
                "该目录读取文件/运行 git，并给出实际证据；\n"
                "- 不要写“等待用户提供代码”，也不要向用户提问或要求确认；\n"
                "- 只给出步骤、检查项、期望结果与判定标准；\n"
                "- 如果任务很简单（例如只输出一行文本），方案必须保持极简，"
                "不要扩展交付物、检查清单或额外字段；\n"
                "- 只做方案，不要修改代码。\n"
                "输出：目标、检查项、期望结果、约束。",
                turn_id=0, timeout=timeout,
                dedupe_key=f"{conversation.run_id}:0:{planner}",
                role="planner", phase=RunPhase.ANALYZING,
            )
            plan = plan_reply.text
            round_no = max(1, round_no)
            workflow.update(plan=plan, stage="work", round=round_no)
            conversation.save_workflow(workflow, event_type="workflow_plan_saved")
            stage = "work"
        elif stage == "work":
            conversation.set_phase(RunPhase.EXECUTING)
        else:
            conversation.set_phase(RunPhase.REVIEWING)

        while True:
            if round_no > max_rounds:
                workflow["stage"] = "max_rounds"
                conversation.save_workflow(workflow, event_type="workflow_max_rounds")
                conversation.finish(
                    RunStatus.FAILED, RunPhase.FAILED,
                    {**metadata, "approved": False, "rounds": round_no - 1,
                     "reason": "max_rounds_exceeded",
                     "review": review.model_dump(mode="json") if review
                     else workflow.get("review_result")},
                )
                return {
                    "run_id": conversation.run_id, "approved": False,
                    "rounds": round_no - 1, "plan": plan, "work": work_text,
                    "review": review_reply.text if review_reply
                    else str(workflow.get("review") or ""),
                    "review_result": review.model_dump(mode="json") if review
                    else workflow.get("review_result"),
                    "error": "达到最大轮数仍未通过审查",
                }

            stage = workflow.get("stage") or stage
            if stage == "work":
                conversation.set_phase(RunPhase.EXECUTING)
                conversation.save_workflow(workflow, event_type="workflow_node_work")
                notes = str(workflow.get("notes") or "")
                resume_note = f"【继续说明】{notes}\n" if notes else ""
                work_reply = await conversation.ask(
                    worker,
                    "你是任务执行者。请严格依据任务方案完成工作。\n"
                    f"{workspace_context}{resume_note}"
                    "涉及代码检查/修改时，直接在工作目录使用工具（读取文件、rg、git 等），"
                    "不要等待用户粘贴代码；只有方案明确不涉及代码时才直接给出文本结果。\n"
                    "如果方案包含对仓库现状的断言，必须先实际读取/执行验证，以工具结果为准。\n"
                    "不要运行可能长时间阻塞或常驻的命令（启动服务、打开浏览器、交互式程序）；"
                    "验证命令应快速退出，单条命令不要超过 60 秒。\n"
                    f"【任务方案】\n{plan}\n"
                    f"【上一轮审查意见】\n{issues or '无'}\n"
                    "要求：说明你实际检查了哪些文件/命令；不要改动任务范围之外的文件。",
                    turn_id=round_no, timeout=timeout,
                    dedupe_key=f"{conversation.run_id}:{round_no}:{worker}",
                    role="worker", phase=RunPhase.EXECUTING,
                )
                work_text = work_reply.text
                workflow.update(work=work_text, stage="review", notes="")
                conversation.save_workflow(workflow, event_type="workflow_work_saved")
                stage = "review"

            conversation.set_phase(RunPhase.REVIEWING)
            conversation.save_workflow(workflow, event_type="workflow_node_review")
            review_reply = await conversation.ask(
                reviewer,
                "你是审查者。请结合任务方案与执行结果进行审查。\n"
                f"{workspace_context}"
                "只检查执行结果是否满足既定方案，不要提出方案之外的新要求；"
                "若满足方案且证据充分，应判定 approved。方案中对仓库现状的断言不一定为真，"
                "请以执行结果中的实际证据为准。如果工作目录已提供，而执行结果声称"
                "“未收到代码”，应视为未完成任务。\n"
                f"【任务方案】\n{plan}\n"
                f"【执行结果】\n{work_text}\n\n"
                f"{REVIEW_CONTRACT}",
                turn_id=round_no, timeout=timeout,
                dedupe_key=f"{conversation.run_id}:{round_no}:{reviewer}",
                role="reviewer", phase=RunPhase.REVIEWING,
            )
            try:
                review = parse_review_result(review_reply.text)
            except ReviewParseError as exc:
                conversation.attach_reply_metadata(review_reply, {
                    "review_parse_error": str(exc),
                })
                workflow["stage"] = "failed"
                workflow["error"] = f"invalid_review: {exc}"
                conversation.save_workflow(workflow, event_type="workflow_review_invalid")
                conversation.finish(
                    RunStatus.FAILED, RunPhase.FAILED,
                    {**metadata, "approved": False, "rounds": round_no,
                     "reason": "invalid_review", "error": str(exc)},
                )
                return {
                    "run_id": conversation.run_id, "approved": False,
                    "rounds": round_no, "plan": plan, "work": work_text,
                    "review": review_reply.text,
                    "review_result": None, "error": f"审查结果结构非法：{exc}",
                }
            conversation.attach_reply_metadata(review_reply, {
                "review_result": review.model_dump(mode="json"),
            })
            workflow["review"] = review_reply.text
            workflow["review_result"] = review.model_dump(mode="json")

            if review.decision == "approved":
                workflow["stage"] = "done"
                conversation.save_workflow(workflow, event_type="workflow_done")
                conversation.set_phase(RunPhase.APPROVED)
                conversation.finish(
                    RunStatus.COMPLETED, RunPhase.COMPLETED,
                    {**metadata, "approved": True, "rounds": round_no,
                     "review": review.model_dump(mode="json")},
                )
                return {
                    "run_id": conversation.run_id, "approved": True,
                    "rounds": round_no, "plan": plan, "work": work_text,
                    "review": review_reply.text,
                    "review_result": review.model_dump(mode="json"),
                }
            if review.decision == "blocked":
                workflow["stage"] = "blocked"
                conversation.save_workflow(workflow, event_type="workflow_blocked")
                conversation.finish(
                    RunStatus.FAILED, RunPhase.FAILED,
                    {**metadata, "approved": False, "rounds": round_no,
                     "reason": "review_blocked",
                     "review": review.model_dump(mode="json")},
                )
                return {
                    "run_id": conversation.run_id, "approved": False,
                    "rounds": round_no, "plan": plan, "work": work_text,
                    "review": review_reply.text,
                    "review_result": review.model_dump(mode="json"),
                    "error": "审查判定为 blocked",
                }

            issues = review_as_prompt(review)
            round_no += 1
            workflow.update(issues=issues, round=round_no, stage="work", work="")
            conversation.save_workflow(workflow, event_type="workflow_revision")
            conversation.set_phase(
                RunPhase.REVISION_REQUIRED, event_type="revision_required",
                payload={"round": round_no - 1},
            )
    except BaseException:
        workflow["error"] = "exception"
        try:
            conversation.save_workflow(workflow, event_type="workflow_exception")
        except Exception:  # noqa: BLE001
            pass
        if conversation.run.status is RunStatus.RUNNING:
            conversation.finish(
                RunStatus.FAILED, RunPhase.FAILED,
                {**metadata, "approved": False, "reason": "exception"},
            )
        raise


def format_transcript(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        stamp = message.created_at.strftime("%H:%M:%S")
        lines.append(
            f"[{stamp}] turn={message.turn_id} {message.from_agent} -> {message.to_agent} "
            f"({message.status.value}): {message.content}"
        )
    return "\n".join(lines)
