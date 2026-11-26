from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..messaging.schema import (
    ALLOWED_PHASE_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    Message,
    MessageStatus,
    Run,
    RunEvent,
    RunPhase,
    RunStatus,
    SessionBinding,
    Step,
    StepState,
    utc_now,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'created',
    current_turn INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, agent)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL DEFAULT 0,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    provider_session_id TEXT,
    provider_message_id TEXT,
    reply_to TEXT,
    dedupe_key TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS steps (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL DEFAULT 0,
    agent TEXT NOT NULL,
    role TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    input_message_id TEXT,
    output_message_id TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    step_id TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe_run
    ON messages (run_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages (run_id, turn_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages (reply_to);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps (run_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events (run_id, seq);
"""

_MESSAGE_COLUMNS = {
    "run_id", "turn_id", "from_agent", "to_agent", "content", "status",
    "provider", "provider_session_id", "provider_message_id", "reply_to",
    "dedupe_key", "attempt", "error", "updated_at", "last_attempt_at", "metadata",
}
_STEP_COLUMNS = {
    "state", "attempt", "input_message_id", "output_message_id", "error",
    "started_at", "finished_at", "metadata",
}
_RUN_COLUMNS = {"status", "phase", "current_turn", "updated_at", "metadata"}

UNFINISHED_STATUSES: tuple[MessageStatus, ...] = (
    MessageStatus.CREATED,
    MessageStatus.SENDING,
    MessageStatus.SENT,
    MessageStatus.ACKNOWLEDGED,
    MessageStatus.UNKNOWN,
    MessageStatus.PENDING,
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    """SQLite 持久化：runs / sessions / messages / steps / run_events。"""

    def __init__(self, path: str | Path = "data/orchestrator.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._ensure_column("runs", "phase", "phase TEXT NOT NULL DEFAULT 'created'")
            self._ensure_column("runs", "current_turn", "current_turn INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("messages", "attempt", "attempt INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("messages", "last_attempt_at", "last_attempt_at TEXT")
            # 旧版本使用全局 dedupe_key 唯一索引；迁移为 (run_id, dedupe_key)。
            self._conn.execute("DROP INDEX IF EXISTS idx_messages_dedupe")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe_run"
                " ON messages (run_id, dedupe_key) WHERE dedupe_key IS NOT NULL"
            )

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- runs -------------------------------------------------------------

    def create_run(self, goal: str = "", run_id: str | None = None,
                   metadata: dict[str, Any] | None = None) -> Run:
        run = Run(goal=goal, metadata=metadata or {})
        if run_id:
            run.run_id = run_id
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, goal, status, phase, current_turn,"
                " created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run.run_id, run.goal, run.status.value, run.phase.value, run.current_turn,
                 _iso(run.created_at), _iso(run.updated_at), _dump(run.metadata)),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def update_run(self, run_id: str, status: RunStatus | None = None,
                   metadata: dict[str, Any] | None = None, *,
                   phase: RunPhase | None = None,
                   current_turn: int | None = None) -> None:
        values: dict[str, Any] = {"updated_at": _iso(utc_now())}
        if status is not None:
            values["status"] = status.value if isinstance(status, RunStatus) else str(status)
        if phase is not None:
            values["phase"] = phase.value if isinstance(phase, RunPhase) else str(phase)
        if current_turn is not None:
            values["current_turn"] = current_turn
        if metadata is not None:
            values["metadata"] = _dump(metadata)
        unknown = set(values) - _RUN_COLUMNS
        if unknown:
            raise ValueError(f"不支持更新的字段: {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*values.values(), run_id),
            )

    def transition_run(self, run_id: str, *, status: RunStatus | None = None,
                       phase: RunPhase | None = None,
                       current_turn: int | None = None,
                       metadata: dict[str, Any] | None = None,
                       event_type: str | None = None,
                       step_id: str | None = None,
                       payload: dict[str, Any] | None = None,
                       validate: bool = True) -> Run:
        """原子地更新 run 状态并写入审计事件，同时校验迁移合法性。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"未知 run_id: {run_id}")
            current_status = RunStatus(row["status"])
            current_phase = RunPhase(row["phase"] or RunPhase.CREATED.value)

            if validate:
                if current_status in TERMINAL_STATUSES:
                    raise InvalidTransition(
                        f"run 已处于终态 {current_status.value}，不允许继续迁移"
                    )
                if phase is not None and phase is not current_phase:
                    allowed = ALLOWED_PHASE_TRANSITIONS.get(current_phase, frozenset())
                    if phase not in allowed:
                        raise InvalidTransition(
                            f"非法迁移：{current_phase.value} -> {phase.value}"
                        )
                if status is not None and status is not current_status:
                    if current_status is not RunStatus.RUNNING:
                        raise InvalidTransition(
                            f"非法状态：{current_status.value} -> {status.value}"
                        )
                    if status not in (RunStatus.COMPLETED, RunStatus.FAILED,
                                      RunStatus.CANCELLED):
                        raise InvalidTransition(
                            f"非法状态：{current_status.value} -> {status.value}"
                        )

            values: dict[str, Any] = {"updated_at": _iso(utc_now())}
            if status is not None:
                values["status"] = status.value
            if phase is not None:
                values["phase"] = phase.value
            if current_turn is not None:
                values["current_turn"] = current_turn
            if metadata is not None:
                values["metadata"] = _dump(metadata)
            assignments = ", ".join(f"{key} = ?" for key in values)
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*values.values(), run_id),
            )
            if event_type:
                target_phase = phase or current_phase
                self._append_event_tx(run_id, event_type, target_phase, step_id, payload or {})
        updated = self.get_run(run_id)
        assert updated is not None
        return updated

    def _append_event_tx(self, run_id: str, event_type: str, phase: RunPhase,
                         step_id: str | None = None,
                         payload: dict[str, Any] | None = None) -> RunEvent:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,),
        ).fetchone()
        event = RunEvent(
            run_id=run_id, seq=int(row[0]) + 1, event_type=event_type,
            phase=phase, step_id=step_id, payload=payload or {},
        )
        self._conn.execute(
            "INSERT INTO run_events (event_id, run_id, seq, event_type, phase, step_id,"
            " created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.run_id, event.seq, event.event_type, event.phase.value,
             event.step_id, _iso(event.created_at), _dump(event.payload)),
        )
        return event

    def list_runs(self, limit: int = 20, *, status: RunStatus | None = None) -> list[Run]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    # ---- sessions ---------------------------------------------------------

    def upsert_binding(self, binding: SessionBinding) -> SessionBinding:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (session_id, run_id, agent, provider, provider_session_id,"
                " created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (run_id, agent) DO UPDATE SET"
                " session_id = excluded.session_id, provider = excluded.provider,"
                " provider_session_id = excluded.provider_session_id, metadata = excluded.metadata",
                (binding.session_id, binding.run_id, binding.agent, binding.provider,
                 binding.provider_session_id, _iso(binding.created_at), _dump(binding.metadata)),
            )
        return binding

    def get_binding(self, run_id: str, agent: str) -> SessionBinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE run_id = ? AND agent = ?", (run_id, agent),
            ).fetchone()
        if not row:
            return None
        return SessionBinding(
            session_id=row["session_id"], run_id=row["run_id"], agent=row["agent"],
            provider=row["provider"], provider_session_id=row["provider_session_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]),
        )

    # ---- messages ---------------------------------------------------------

    def save_message(self, message: Message) -> Message:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages (message_id, run_id, turn_id, from_agent, to_agent, content,"
                " status, provider, provider_session_id, provider_message_id, reply_to, dedupe_key,"
                " attempt, error, created_at, updated_at, last_attempt_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message.message_id, message.run_id, message.turn_id, message.from_agent,
                 message.to_agent, message.content, message.status.value, message.provider,
                 message.provider_session_id, message.provider_message_id, message.reply_to,
                 message.dedupe_key, message.attempt, message.error, _iso(message.created_at),
                 _iso(message.updated_at), _iso(message.last_attempt_at), _dump(message.metadata)),
            )
        return message

    def update_message(self, message_id: str, **fields: Any) -> None:
        unknown = set(fields) - _MESSAGE_COLUMNS
        if unknown:
            raise ValueError(f"不支持更新的字段: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = dict(fields)
        values["updated_at"] = _iso(utc_now())
        if "status" in values and isinstance(values["status"], MessageStatus):
            values["status"] = values["status"].value
        if "last_attempt_at" in values and isinstance(values["last_attempt_at"], datetime):
            values["last_attempt_at"] = _iso(values["last_attempt_at"])
        if "metadata" in values:
            values["metadata"] = _dump(values["metadata"])
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE messages SET {assignments} WHERE message_id = ?",
                (*values.values(), message_id),
            )

    def get_message(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,),
            ).fetchone()
        return self._row_to_message(row) if row else None

    def find_by_dedupe(self, run_id: str, dedupe_key: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE run_id = ? AND dedupe_key = ?",
                (run_id, dedupe_key),
            ).fetchone()
        return self._row_to_message(row) if row else None

    def find_reply(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE reply_to = ? ORDER BY created_at LIMIT 1", (message_id,),
            ).fetchone()
        return self._row_to_message(row) if row else None

    def list_messages(self, run_id: str) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE run_id = ? ORDER BY turn_id, created_at", (run_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_unfinished_messages(self, run_id: str) -> list[Message]:
        placeholders = ",".join("?" for _ in UNFINISHED_STATUSES)
        statuses = [status.value for status in UNFINISHED_STATUSES]
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.* FROM messages m"
                f" WHERE m.run_id = ? AND m.status IN ({placeholders})"
                " AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.reply_to = m.message_id)"
                " ORDER BY m.turn_id, m.created_at",
                (run_id, *statuses),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_unfinished(self, *, provider: str | None = None) -> list[Message]:
        """列出所有 run 中未完成、且可能仍占用远端资源的消息。"""
        placeholders = ",".join("?" for _ in UNFINISHED_STATUSES)
        params: list[Any] = [status.value for status in UNFINISHED_STATUSES]
        query = (
            "SELECT m.* FROM messages m"
            f" WHERE m.status IN ({placeholders})"
            " AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.reply_to = m.message_id)"
        )
        if provider:
            query += " AND m.provider = ?"
            params.append(provider)
        query += " ORDER BY m.created_at"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_message(row) for row in rows]

    def complete_exchange(self, *, outgoing_message_id: str, reply: Message,
                          step_id: str | None, run_id: str,
                          current_turn: int) -> None:
        """原子完成：写入回复、标记原消息、收尾 step、更新 run、写事件。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (outgoing_message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"未知 message_id: {outgoing_message_id}")
            if row["run_id"] != run_id:
                raise ValueError(
                    f"消息 {outgoing_message_id} 不属于 run {run_id}"
                )
            if reply.run_id != run_id or reply.reply_to != outgoing_message_id:
                raise ValueError("回复消息的 run_id/reply_to 与原始消息不一致")
            if step_id:
                step_row = self._conn.execute(
                    "SELECT * FROM steps WHERE step_id = ?", (step_id,),
                ).fetchone()
                if step_row is None:
                    raise KeyError(f"未知 step_id: {step_id}")
                if step_row["run_id"] != run_id:
                    raise ValueError(f"step {step_id} 不属于 run {run_id}")
                if step_row["input_message_id"] not in (None, outgoing_message_id):
                    raise ValueError(f"step {step_id} 与消息 {outgoing_message_id} 不匹配")
            existing = self._conn.execute(
                "SELECT 1 FROM messages WHERE reply_to = ?", (outgoing_message_id,),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("该消息已有回复，拒绝重复完成（避免覆盖恢复结果）")

            self._insert_message_tx(reply)
            now = _iso(utc_now())
            self._conn.execute(
                "UPDATE messages SET status = ?, updated_at = ? WHERE message_id = ?",
                (MessageStatus.COMPLETED.value, now, outgoing_message_id),
            )
            if step_id:
                self._conn.execute(
                    "UPDATE steps SET state = ?, finished_at = ?, error = NULL,"
                    " output_message_id = ? WHERE step_id = ?",
                    (StepState.COMPLETED.value, now, reply.message_id, step_id),
                )
            run_row = self._conn.execute(
                "SELECT phase FROM runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(f"未知 run_id: {run_id}")
            self._conn.execute(
                "UPDATE runs SET current_turn = ?, updated_at = ? WHERE run_id = ?",
                (current_turn, now, run_id),
            )
            phase = RunPhase(run_row["phase"])
            payload = {"message_id": outgoing_message_id,
                       "reply_message_id": reply.message_id}
            self._append_event_tx(run_id, "message_completed", phase, step_id, payload)
            if step_id:
                self._append_event_tx(run_id, "step_completed", phase, step_id, payload)

    def release_message(self, run_id: str, message_id: str, *, reason: str) -> Message:
        """人工解除一条不确定消息：标记 CANCELLED、收尾 step、写审计事件。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"未知 message_id: {message_id}")
            if row["run_id"] != run_id:
                raise ValueError(f"消息 {message_id} 不属于 run {run_id}")
            metadata = json.loads(row["metadata"])
            step_id = metadata.get("step_id")
            now = _iso(utc_now())
            self._conn.execute(
                "UPDATE messages SET status = ?, error = ?, updated_at = ? WHERE message_id = ?",
                (MessageStatus.CANCELLED.value, reason, now, message_id),
            )
            if step_id:
                self._conn.execute(
                    "UPDATE steps SET state = ?, finished_at = ?, error = ? WHERE step_id = ?",
                    (StepState.FAILED.value, now, reason, step_id),
                )
            run_row = self._conn.execute(
                "SELECT phase FROM runs WHERE run_id = ?", (row["run_id"],),
            ).fetchone()
            phase = RunPhase(run_row["phase"]) if run_row else RunPhase.CREATED
            self._append_event_tx(row["run_id"], "message_released", phase, step_id,
                                  {"message_id": message_id, "reason": reason})
        released = self.get_message(message_id)
        assert released is not None
        return released

    def _insert_message_tx(self, message: Message) -> None:
        self._conn.execute(
            "INSERT INTO messages (message_id, run_id, turn_id, from_agent, to_agent, content,"
            " status, provider, provider_session_id, provider_message_id, reply_to, dedupe_key,"
            " attempt, error, created_at, updated_at, last_attempt_at, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.run_id, message.turn_id, message.from_agent,
             message.to_agent, message.content, message.status.value, message.provider,
             message.provider_session_id, message.provider_message_id, message.reply_to,
             message.dedupe_key, message.attempt, message.error, _iso(message.created_at),
             _iso(message.updated_at), _iso(message.last_attempt_at), _dump(message.metadata)),
        )

    # ---- steps ------------------------------------------------------------

    def create_step(self, step: Step) -> Step:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO steps (step_id, run_id, turn_id, agent, role, state, attempt,"
                " input_message_id, output_message_id, error, started_at, finished_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (step.step_id, step.run_id, step.turn_id, step.agent, step.role,
                 step.state.value, step.attempt, step.input_message_id, step.output_message_id,
                 step.error, _iso(step.started_at), _iso(step.finished_at), _dump(step.metadata)),
            )
        return step

    def update_step(self, step_id: str, **fields: Any) -> None:
        unknown = set(fields) - _STEP_COLUMNS
        if unknown:
            raise ValueError(f"不支持更新的字段: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = dict(fields)
        if "state" in values and isinstance(values["state"], StepState):
            values["state"] = values["state"].value
        for key in ("started_at", "finished_at"):
            if key in values and isinstance(values[key], datetime):
                values[key] = _iso(values[key])
        if "metadata" in values:
            values["metadata"] = _dump(values["metadata"])
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE steps SET {assignments} WHERE step_id = ?",
                (*values.values(), step_id),
            )

    def start_step(self, step_id: str) -> None:
        self.update_step(step_id, state=StepState.RUNNING, started_at=utc_now())

    def finish_step(self, step_id: str, state: StepState, *, error: str | None = None,
                    output_message_id: str | None = None) -> None:
        self.update_step(step_id, state=state, finished_at=utc_now(), error=error,
                         output_message_id=output_message_id)

    def get_step(self, step_id: str) -> Step | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM steps WHERE step_id = ?", (step_id,)).fetchone()
        return self._row_to_step(row) if row else None

    def list_steps(self, run_id: str) -> list[Step]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY turn_id, started_at", (run_id,),
            ).fetchall()
        return [self._row_to_step(row) for row in rows]

    # ---- events -----------------------------------------------------------

    def append_event(self, run_id: str, event_type: str, phase: RunPhase,
                     step_id: str | None = None,
                     payload: dict[str, Any] | None = None) -> RunEvent:
        with self._lock, self._conn:
            return self._append_event_tx(run_id, event_type, phase, step_id, payload or {})

    def list_events(self, run_id: str, limit: int = 500) -> list[RunEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq LIMIT ?", (run_id, limit),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    # ---- mapping ----------------------------------------------------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"], goal=row["goal"], status=RunStatus(row["status"]),
            phase=RunPhase(row["phase"]) if row["phase"] else RunPhase.CREATED,
            current_turn=row["current_turn"] or 0,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        keys = row.keys()
        return Message(
            message_id=row["message_id"], run_id=row["run_id"], turn_id=row["turn_id"],
            from_agent=row["from_agent"], to_agent=row["to_agent"], content=row["content"],
            status=MessageStatus(row["status"]), provider=row["provider"],
            provider_session_id=row["provider_session_id"],
            provider_message_id=row["provider_message_id"], reply_to=row["reply_to"],
            dedupe_key=row["dedupe_key"], error=row["error"],
            attempt=(row["attempt"] if "attempt" in keys else 0) or 0,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_attempt_at=_parse_dt(row["last_attempt_at"] if "last_attempt_at" in keys else None),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_step(row: sqlite3.Row) -> Step:
        return Step(
            step_id=row["step_id"], run_id=row["run_id"], turn_id=row["turn_id"],
            agent=row["agent"], role=row["role"], state=StepState(row["state"]),
            attempt=row["attempt"], input_message_id=row["input_message_id"],
            output_message_id=row["output_message_id"], error=row["error"],
            started_at=_parse_dt(row["started_at"]), finished_at=_parse_dt(row["finished_at"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            event_id=row["event_id"], run_id=row["run_id"], seq=row["seq"],
            event_type=row["event_type"], phase=RunPhase(row["phase"]),
            step_id=row["step_id"], created_at=datetime.fromisoformat(row["created_at"]),
            payload=json.loads(row["payload"]),
        )


def iter_transcript(messages: Iterable[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        lines.append(f"[{message.status.value}] {message.from_agent} -> {message.to_agent}: {message.content}")
    return "\n".join(lines)
