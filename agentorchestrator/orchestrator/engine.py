from __future__ import annotations

from typing import Any

from ..adapters.base import AgentAdapter, AgentReply
from ..messaging.schema import (
    Message,
    MessageStatus,
    Run,
    RunStatus,
    SessionBinding,
    utc_now,
)
from ..storage.database import Store


class Conversation:
    """协调器核心：管理 run、agent 会话映射与统一消息记录。"""

    def __init__(self, store: Store, adapters: dict[str, AgentAdapter], *,
                 goal: str = "", run_id: str | None = None) -> None:
        self.store = store
        self.adapters = adapters
        self.run = store.create_run(goal=goal, run_id=run_id)
        self.turn = 0

    @property
    def run_id(self) -> str:
        return self.run.run_id

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
        return binding

    async def ask(self, agent: str, text: str, *, to_agent: str = "orchestrator",
                  turn_id: int | None = None, dedupe_key: str | None = None,
                  timeout: float | None = None) -> AgentReply:
        adapter = self.adapter(agent)

        if dedupe_key:
            existing = self.store.find_by_dedupe(dedupe_key)
            if existing is not None:
                reply = self.store.find_reply(existing.message_id)
                if reply is None:
                    raise RuntimeError(
                        f"幂等键 {dedupe_key} 命中的消息尚未完成响应，请勿重复提交。"
                    )
                return AgentReply(
                    session_id=existing.provider_session_id or "",
                    text=reply.content,
                    provider_message_id=reply.provider_message_id,
                    metadata={"deduplicated": True, **reply.metadata},
                )

        binding = await self.session_for(agent)
        turn = self.turn if turn_id is None else turn_id
        outgoing = Message(
            run_id=self.run_id, turn_id=turn, from_agent="orchestrator", to_agent=agent,
            content=text, status=MessageStatus.PENDING, provider=adapter.kind,
            provider_session_id=binding.provider_session_id, dedupe_key=dedupe_key,
        )
        self.store.save_message(outgoing)

        try:
            ref = await adapter.send_message(
                binding.provider_session_id, text, timeout=timeout,
            )
            self.store.update_message(
                outgoing.message_id, status=MessageStatus.SENT,
                provider_message_id=ref.message_id,
                metadata={**outgoing.metadata, "ref": ref.metadata},
            )
            reply = await adapter.wait_response(ref, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 记录失败再抛出
            self.store.update_message(
                outgoing.message_id, status=MessageStatus.FAILED, error=str(exc),
            )
            raise

        self.store.update_message(outgoing.message_id, status=MessageStatus.COMPLETED)
        reply_message = Message(
            run_id=self.run_id, turn_id=turn, from_agent=agent, to_agent=to_agent,
            content=reply.text, status=MessageStatus.COMPLETED, provider=adapter.kind,
            provider_session_id=binding.provider_session_id,
            provider_message_id=reply.provider_message_id,
            reply_to=outgoing.message_id,
            metadata={"stop_reason": reply.stop_reason, "usage": reply.usage},
        )
        self.store.save_message(reply_message)
        self.turn = max(self.turn, turn + 1)
        return reply

    def transcript(self) -> list[Message]:
        return self.store.list_messages(self.run_id)

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
    """阶段一默认流程：分析 -> 执行 -> 审查，失败则回退给执行者。"""
    reviewer = reviewer or planner
    try:
        plan_reply = await conversation.ask(
            planner,
            "你是需求分析与方案设计者。请针对下面的协作任务形成结构化任务方案。\n"
            f"任务：{goal}\n"
            "输出：目标、检查项、期望输出字段、约束。只做方案，不要修改代码。",
            turn_id=0, timeout=timeout, dedupe_key=f"{conversation.run_id}:0:{planner}",
        )
        plan = plan_reply.text
        issues = ""
        work_reply: AgentReply | None = None
        review_reply: AgentReply | None = None
        approved = False
        rounds = 0

        for round_no in range(1, max_rounds + 1):
            rounds = round_no
            work_reply = await conversation.ask(
                worker,
                "你是代码执行者。请依据任务方案检查本地项目并按需修改。\n"
                f"【任务方案】\n{plan}\n"
                f"【上一轮审查意见】\n{issues or '无'}\n"
                "要求：说明检查了什么、发现或修改了什么；不要改动任务范围之外的文件。",
                turn_id=round_no, timeout=timeout,
                dedupe_key=f"{conversation.run_id}:{round_no}:{worker}",
            )
            review_reply = await conversation.ask(
                reviewer,
                "你是审查者。请结合任务方案与执行结果进行审查。\n"
                f"【任务方案】\n{plan}\n"
                f"【执行结果】\n{work_reply.text}\n"
                "若结果完整合理，请以 APPROVED 开头；否则列出具体问题与修改建议。",
                turn_id=round_no, timeout=timeout,
                dedupe_key=f"{conversation.run_id}:{round_no}:{reviewer}",
            )
            if "APPROVED" in review_reply.text.upper():
                approved = True
                break
            issues = review_reply.text

        conversation.store.update_run(
            conversation.run_id,
            RunStatus.COMPLETED,
            metadata={"approved": approved, "rounds": rounds, "finished_at": utc_now().isoformat()},
        )
        return {
            "run_id": conversation.run_id,
            "approved": approved,
            "rounds": rounds,
            "plan": plan,
            "work": work_reply.text if work_reply else "",
            "review": review_reply.text if review_reply else "",
        }
    except BaseException:
        conversation.store.update_run(
            conversation.run_id, RunStatus.FAILED,
            metadata={"finished_at": utc_now().isoformat()},
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
