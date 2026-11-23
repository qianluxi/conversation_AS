from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..messaging.schema import (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages (dedupe_key) WHERE dedupe_key IS NOT NULL;
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

    def find_by_dedupe(self, dedupe_key: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE dedupe_key = ?", (dedupe_key,),
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
