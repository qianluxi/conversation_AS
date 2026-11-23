from __future__ import annotations

from typing import Any

from ..adapters.base import (
    AdapterError,
    AdapterTimeout,
    AgentAdapter,
    AgentReply,
    MessageRef,
)
from ..messaging.schema import (
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
        self.store.update_run(self.run_id, phase=phase, current_turn=self.turn)
        self._refresh()
        self.event(event_type, payload=payload or {"phase": phase.value})

    def finish(self, status: RunStatus, phase: RunPhase,
               metadata: dict[str, Any] | None = None) -> None:
        merged = {**self.run.metadata, **(metadata or {})}
        merged.setdefault("finished_at", utc_now().isoformat())
        self.store.update_run(self.run_id, status=status, phase=phase,
                             current_turn=self.turn, metadata=merged)
        self._refresh()
        self.event(f"run_{status.value}", phase=phase, payload=metadata or {})

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
            existing = self.store.find_by_dedupe(dedupe_key)
            if existing is not None:
                reply = self.store.find_reply(existing.message_id)
                if reply is None and existing.status is MessageStatus.FAILED:
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
        except AdapterTimeout as exc:
            self._mark_unknown(outgoing, step, exc, phase="send_timeout")
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
        self.store.update_message(outgoing.message_id, status=MessageStatus.COMPLETED)
        reply_message = Message(
            run_id=self.run_id, turn_id=outgoing.turn_id, from_agent=outgoing.to_agent,
            to_agent=outgoing.from_agent, content=reply.text,
            status=MessageStatus.COMPLETED, provider=binding.provider,
            provider_session_id=binding.provider_session_id,
            provider_message_id=reply.provider_message_id,
            reply_to=outgoing.message_id,
            metadata={"stop_reason": reply.stop_reason, "usage": reply.usage},
        )
        self.store.save_message(reply_message)
        if step is not None:
            self.store.finish_step(step.step_id, StepState.COMPLETED,
                                   output_message_id=reply_message.message_id)
            self.event("step_completed", step_id=step.step_id, payload={
                "message_id": outgoing.message_id,
                "output_message_id": reply_message.message_id,
            })
        self.event("message_completed", step_id=step.step_id if step else None,
                   payload={"message_id": outgoing.message_id,
                            "reply_message_id": reply_message.message_id})

        self.turn = max(self.turn, outgoing.turn_id + 1)
        self.store.update_run(self.run_id, current_turn=self.turn)
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

    async def resume(self, *, timeout: float | None = None,
                     allow_resend_unstarted: bool = False) -> list[dict[str, Any]]:
        """核对未完成消息的外部状态；默认绝不重发可能已执行的消息。"""
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
                self.store.finish_step(
                    step.step_id, StepState.COMPLETED,
                    output_message_id=reply.message_id if reply else None,
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
                            timeout: float | None = None) -> dict[str, Any]:
    """结构化流程：分析 -> 执行 -> 结构化审查，失败则回退给执行者。"""
    reviewer = reviewer or planner
    metadata: dict[str, Any] = {"planner": planner, "worker": worker, "reviewer": reviewer}
    try:
        conversation.set_phase(RunPhase.ANALYZING)
        plan_reply = await conversation.ask(
            planner,
            "你是需求分析与方案设计者。请针对下面的协作任务形成结构化任务方案。\n"
            f"任务：{goal}\n"
            "输出：目标、检查项、期望输出字段、约束。只做方案，不要修改代码。",
            turn_id=0, timeout=timeout, dedupe_key=f"{conversation.run_id}:0:{planner}",
            role="planner", phase=RunPhase.ANALYZING,
        )
        plan = plan_reply.text
        issues = ""
        work_reply: AgentReply | None = None
        review_reply: AgentReply | None = None
        review: ReviewResult | None = None
        rounds = 0

        for round_no in range(1, max_rounds + 1):
            rounds = round_no
            conversation.set_phase(RunPhase.EXECUTING)
            work_reply = await conversation.ask(
                worker,
                "你是代码执行者。请依据任务方案检查本地项目并按需修改。\n"
                f"【任务方案】\n{plan}\n"
                f"【上一轮审查意见】\n{issues or '无'}\n"
                "要求：说明检查了什么、发现或修改了什么；不要改动任务范围之外的文件。",
                turn_id=round_no, timeout=timeout,
                dedupe_key=f"{conversation.run_id}:{round_no}:{worker}",
                role="worker", phase=RunPhase.EXECUTING,
            )
            conversation.set_phase(RunPhase.REVIEWING)
            review_reply = await conversation.ask(
                reviewer,
                "你是审查者。请结合任务方案与执行结果进行审查。\n"
                f"【任务方案】\n{plan}\n"
                f"【执行结果】\n{work_reply.text}\n\n"
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
                conversation.finish(
                    RunStatus.FAILED, RunPhase.FAILED,
                    {**metadata, "approved": False, "rounds": rounds,
                     "reason": "invalid_review", "error": str(exc)},
                )
                return {
                    "run_id": conversation.run_id, "approved": False, "rounds": rounds,
                    "plan": plan, "work": work_reply.text, "review": review_reply.text,
                    "review_result": None, "error": f"审查结果结构非法：{exc}",
                }
            conversation.attach_reply_metadata(review_reply, {
                "review_result": review.model_dump(mode="json"),
            })

            if review.decision == "approved":
                conversation.finish(
                    RunStatus.COMPLETED, RunPhase.APPROVED,
                    {**metadata, "approved": True, "rounds": rounds,
                     "review": review.model_dump(mode="json")},
                )
                return {
                    "run_id": conversation.run_id, "approved": True, "rounds": rounds,
                    "plan": plan, "work": work_reply.text, "review": review_reply.text,
                    "review_result": review.model_dump(mode="json"),
                }
            if review.decision == "blocked":
                conversation.finish(
                    RunStatus.FAILED, RunPhase.FAILED,
                    {**metadata, "approved": False, "rounds": rounds,
                     "reason": "review_blocked",
                     "review": review.model_dump(mode="json")},
                )
                return {
                    "run_id": conversation.run_id, "approved": False, "rounds": rounds,
                    "plan": plan, "work": work_reply.text, "review": review_reply.text,
                    "review_result": review.model_dump(mode="json"),
                    "error": "审查判定为 blocked",
                }

            conversation.set_phase(RunPhase.REVISION_REQUIRED, event_type="revision_required",
                                   payload={"round": round_no})
            issues = review_as_prompt(review)

        reason = "max_rounds_exceeded"
        conversation.finish(
            RunStatus.FAILED, RunPhase.FAILED,
            {**metadata, "approved": False, "rounds": rounds, "reason": reason,
             "review": review.model_dump(mode="json") if review else None},
        )
        return {
            "run_id": conversation.run_id, "approved": False, "rounds": rounds,
            "plan": plan, "work": work_reply.text if work_reply else "",
            "review": review_reply.text if review_reply else "",
            "review_result": review.model_dump(mode="json") if review else None,
            "error": "达到最大轮数仍未通过审查",
        }
    except BaseException:
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
