from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..messaging.schema import Message, MessageStatus, Run, RunStatus, SessionBinding, utc_now


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
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
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages (run_id, turn_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages (reply_to);
"""

_COLUMNS = {
    "run_id",
    "turn_id",
    "from_agent",
    "to_agent",
    "content",
    "status",
    "provider",
    "provider_session_id",
    "provider_message_id",
    "reply_to",
    "dedupe_key",
    "error",
    "updated_at",
    "metadata",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class Store:
    """SQLite 持久化：runs / sessions / messages。同步 API，内部加锁。"""

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
                "INSERT INTO runs (run_id, goal, status, created_at, updated_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run.run_id, run.goal, run.status.value, _iso(run.created_at),
                 _iso(run.updated_at), _dump(run.metadata)),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def update_run(self, run_id: str, status: RunStatus, metadata: dict[str, Any] | None = None) -> None:
        now = _iso(utc_now())
        with self._lock, self._conn:
            if metadata is None:
                self._conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (status.value, now, run_id),
                )
            else:
                self._conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ?, metadata = ? WHERE run_id = ?",
                    (status.value, now, _dump(metadata), run_id),
                )

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
                " error, created_at, updated_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message.message_id, message.run_id, message.turn_id, message.from_agent,
                 message.to_agent, message.content, message.status.value, message.provider,
                 message.provider_session_id, message.provider_message_id, message.reply_to,
                 message.dedupe_key, message.error, _iso(message.created_at),
                 _iso(message.updated_at), _dump(message.metadata)),
            )
        return message

    def update_message(self, message_id: str, **fields: Any) -> None:
        unknown = set(fields) - _COLUMNS
        if unknown:
            raise ValueError(f"不支持更新的字段: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = dict(fields)
        values["updated_at"] = _iso(utc_now())
        for key in ("status",):
            if key in values and isinstance(values[key], MessageStatus):
                values[key] = values[key].value
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

    def list_runs(self, limit: int = 20) -> list[Run]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    # ---- mapping ----------------------------------------------------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"], goal=row["goal"], status=RunStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            message_id=row["message_id"], run_id=row["run_id"], turn_id=row["turn_id"],
            from_agent=row["from_agent"], to_agent=row["to_agent"], content=row["content"],
            status=MessageStatus(row["status"]), provider=row["provider"],
            provider_session_id=row["provider_session_id"],
            provider_message_id=row["provider_message_id"], reply_to=row["reply_to"],
            dedupe_key=row["dedupe_key"], error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
        )


def iter_transcript(messages: Iterable[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        lines.append(f"[{message.status.value}] {message.from_agent} -> {message.to_agent}: {message.content}")
    return "\n".join(lines)
