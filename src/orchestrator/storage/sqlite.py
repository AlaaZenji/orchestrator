"""SQLite storage backend with WAL mode, foreign keys, and atomic sequencing."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional
from orchestrator.domain.models import Work, Execution, Lease, DomainEvent
from orchestrator.domain.status import WorkStatus, ExecutionStatus
from orchestrator.storage.interface import StorageBackend


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


class SQLiteStorageBackend(StorageBackend):
    """Production-grade SQLite storage backend."""

    SCHEMA = """
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;
    PRAGMA busy_timeout = 5000;

    CREATE TABLE IF NOT EXISTS orchestrator_sequence (
        name TEXT PRIMARY KEY,
        val  INTEGER NOT NULL
    );
    INSERT OR IGNORE INTO orchestrator_sequence (name, val) VALUES ('fencing', 0);

    CREATE TABLE IF NOT EXISTS orchestrator_work (
        work_id                   TEXT PRIMARY KEY,
        title                     TEXT NOT NULL,
        description               TEXT NOT NULL,
        status                    TEXT NOT NULL,
        priority                  INTEGER NOT NULL DEFAULT 0,
        max_retries               INTEGER NOT NULL DEFAULT 3,
        retry_count               INTEGER NOT NULL DEFAULT 0,
        timeout_seconds           INTEGER NOT NULL DEFAULT 3600,
        heartbeat_timeout_seconds INTEGER NOT NULL DEFAULT 180,
        dependencies_json         TEXT NOT NULL DEFAULT '[]',
        labels_json               TEXT NOT NULL DEFAULT '[]',
        inputs_json               TEXT NOT NULL DEFAULT '{}',
        active_fencing_token      INTEGER,
        active_execution_id       TEXT,
        tenant_id                 TEXT NOT NULL DEFAULT 'default',
        version                   INTEGER NOT NULL DEFAULT 1,
        created_at                TEXT NOT NULL,
        updated_at                TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orchestrator_execution (
        execution_id   TEXT PRIMARY KEY,
        work_id        TEXT NOT NULL,
        attempt        INTEGER NOT NULL,
        fencing_token  INTEGER NOT NULL,
        status         TEXT NOT NULL,
        worker_id      TEXT NOT NULL,
        runtime_type   TEXT NOT NULL,
        workspace_path TEXT,
        exit_code      INTEGER,
        error_message  TEXT,
        metadata_json  TEXT NOT NULL DEFAULT '{}',
        started_at     TEXT NOT NULL,
        ended_at       TEXT,
        FOREIGN KEY(work_id) REFERENCES orchestrator_work(work_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS orchestrator_lease (
        work_id          TEXT PRIMARY KEY,
        holder_id        TEXT NOT NULL,
        fencing_token    INTEGER NOT NULL,
        lease_expires_at TEXT NOT NULL,
        acquired_at      TEXT NOT NULL,
        tenant_id        TEXT NOT NULL DEFAULT 'default',
        FOREIGN KEY(work_id) REFERENCES orchestrator_work(work_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS orchestrator_event (
        event_id        TEXT PRIMARY KEY,
        work_id         TEXT,
        execution_id    TEXT,
        event_type      TEXT NOT NULL,
        specversion     TEXT NOT NULL DEFAULT '1.0',
        source          TEXT NOT NULL,
        sequence_number INTEGER NOT NULL,
        data_json       TEXT NOT NULL,
        correlation_id  TEXT,
        causation_id    TEXT,
        occurred_at     TEXT NOT NULL,
        applied_at      TEXT
    );
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._failpoints: dict[str, Callable[[], None]] = {}
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(self.SCHEMA)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def set_failpoint(self, name: str, hook: Optional[Callable[[], None]]) -> None:
        if hook is None:
            self._failpoints.pop(name, None)
        else:
            self._failpoints[name] = hook

    def _trigger_failpoint(self, name: str) -> None:
        if name in self._failpoints:
            self._failpoints[name]()

    async def get_work(self, work_id: str) -> Optional[Work]:
        def _get():
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM orchestrator_work WHERE work_id = ?", (work_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return Work(
                    work_id=row["work_id"],
                    title=row["title"],
                    description=row["description"],
                    status=WorkStatus(row["status"]),
                    priority=row["priority"],
                    max_retries=row["max_retries"],
                    retry_count=row["retry_count"],
                    timeout_seconds=row["timeout_seconds"],
                    heartbeat_timeout_seconds=row["heartbeat_timeout_seconds"],
                    dependencies=tuple(json.loads(row["dependencies_json"])),
                    labels=tuple(json.loads(row["labels_json"])),
                    inputs=json.loads(row["inputs_json"]),
                    active_fencing_token=row["active_fencing_token"],
                    active_execution_id=row["active_execution_id"],
                    tenant_id=row["tenant_id"],
                    version=row["version"],
                    created_at=parse_iso(row["created_at"]),
                    updated_at=parse_iso(row["updated_at"]),
                )
        return await asyncio.to_thread(_get)

    async def save_work(self, work: Work) -> None:
        self._trigger_failpoint("before_save_work")
        def _save():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orchestrator_work (
                        work_id, title, description, status, priority, max_retries, retry_count,
                        timeout_seconds, heartbeat_timeout_seconds, dependencies_json, labels_json,
                        inputs_json, active_fencing_token, active_execution_id, tenant_id,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(work_id) DO UPDATE SET
                        title = excluded.title,
                        description = excluded.description,
                        status = excluded.status,
                        priority = excluded.priority,
                        max_retries = excluded.max_retries,
                        retry_count = excluded.retry_count,
                        timeout_seconds = excluded.timeout_seconds,
                        heartbeat_timeout_seconds = excluded.heartbeat_timeout_seconds,
                        dependencies_json = excluded.dependencies_json,
                        labels_json = excluded.labels_json,
                        inputs_json = excluded.inputs_json,
                        active_fencing_token = excluded.active_fencing_token,
                        active_execution_id = excluded.active_execution_id,
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (
                        work.work_id, work.title, work.description, work.status.value,
                        work.priority, work.max_retries, work.retry_count, work.timeout_seconds,
                        work.heartbeat_timeout_seconds, json.dumps(list(work.dependencies)),
                        json.dumps(list(work.labels)), json.dumps(work.inputs),
                        work.active_fencing_token, work.active_execution_id, work.tenant_id,
                        work.version, work.created_at.isoformat(), work.updated_at.isoformat(),
                    ),
                )
        await asyncio.to_thread(_save)
        self._trigger_failpoint("after_save_work")

    async def list_works(self, tenant_id: str = "default") -> List[Work]:
        def _list():
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM orchestrator_work WHERE tenant_id = ?", (tenant_id,))
                res = []
                for row in cur.fetchall():
                    res.append(
                        Work(
                            work_id=row["work_id"],
                            title=row["title"],
                            description=row["description"],
                            status=WorkStatus(row["status"]),
                            priority=row["priority"],
                            max_retries=row["max_retries"],
                            retry_count=row["retry_count"],
                            timeout_seconds=row["timeout_seconds"],
                            heartbeat_timeout_seconds=row["heartbeat_timeout_seconds"],
                            dependencies=tuple(json.loads(row["dependencies_json"])),
                            labels=tuple(json.loads(row["labels_json"])),
                            inputs=json.loads(row["inputs_json"]),
                            active_fencing_token=row["active_fencing_token"],
                            active_execution_id=row["active_execution_id"],
                            tenant_id=row["tenant_id"],
                            version=row["version"],
                            created_at=parse_iso(row["created_at"]),
                            updated_at=parse_iso(row["updated_at"]),
                        )
                    )
                return res
        return await asyncio.to_thread(_list)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        def _get():
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM orchestrator_execution WHERE execution_id = ?", (execution_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return Execution(
                    execution_id=row["execution_id"],
                    work_id=row["work_id"],
                    attempt=row["attempt"],
                    fencing_token=row["fencing_token"],
                    status=ExecutionStatus(row["status"]),
                    worker_id=row["worker_id"],
                    runtime_type=row["runtime_type"],
                    workspace_path=row["workspace_path"],
                    exit_code=row["exit_code"],
                    error_message=row["error_message"],
                    metadata=json.loads(row["metadata_json"]),
                    started_at=parse_iso(row["started_at"]),
                    ended_at=parse_iso(row["ended_at"]),
                )
        return await asyncio.to_thread(_get)

    async def save_execution(self, execution: Execution) -> None:
        def _save():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orchestrator_execution (
                        execution_id, work_id, attempt, fencing_token, status, worker_id,
                        runtime_type, workspace_path, exit_code, error_message, metadata_json,
                        started_at, ended_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id) DO UPDATE SET
                        status = excluded.status,
                        exit_code = excluded.exit_code,
                        error_message = excluded.error_message,
                        metadata_json = excluded.metadata_json,
                        ended_at = excluded.ended_at
                    """,
                    (
                        execution.execution_id, execution.work_id, execution.attempt,
                        execution.fencing_token, execution.status.value, execution.worker_id,
                        execution.runtime_type, execution.workspace_path, execution.exit_code,
                        execution.error_message, json.dumps(execution.metadata),
                        execution.started_at.isoformat(),
                        execution.ended_at.isoformat() if execution.ended_at else None,
                    ),
                )
        await asyncio.to_thread(_save)

    async def list_executions(self, work_id: str) -> List[Execution]:
        def _list():
            with self._get_connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM orchestrator_execution WHERE work_id = ? ORDER BY attempt ASC",
                    (work_id,),
                )
                res = []
                for row in cur.fetchall():
                    res.append(
                        Execution(
                            execution_id=row["execution_id"],
                            work_id=row["work_id"],
                            attempt=row["attempt"],
                            fencing_token=row["fencing_token"],
                            status=ExecutionStatus(row["status"]),
                            worker_id=row["worker_id"],
                            runtime_type=row["runtime_type"],
                            workspace_path=row["workspace_path"],
                            exit_code=row["exit_code"],
                            error_message=row["error_message"],
                            metadata=json.loads(row["metadata_json"]),
                            started_at=parse_iso(row["started_at"]),
                            ended_at=parse_iso(row["ended_at"]),
                        )
                    )
                return res
        return await asyncio.to_thread(_list)

    async def get_lease(self, work_id: str) -> Optional[Lease]:
        def _get():
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM orchestrator_lease WHERE work_id = ?", (work_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return Lease(
                    work_id=row["work_id"],
                    holder_id=row["holder_id"],
                    fencing_token=row["fencing_token"],
                    expires_at=parse_iso(row["lease_expires_at"]),
                    acquired_at=parse_iso(row["acquired_at"]),
                    tenant_id=row["tenant_id"],
                )
        return await asyncio.to_thread(_get)

    async def save_lease(self, lease: Lease) -> None:
        def _save():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orchestrator_lease (
                        work_id, holder_id, fencing_token, lease_expires_at, acquired_at, tenant_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(work_id) DO UPDATE SET
                        holder_id = excluded.holder_id,
                        fencing_token = excluded.fencing_token,
                        lease_expires_at = excluded.lease_expires_at,
                        acquired_at = excluded.acquired_at
                    """,
                    (
                        lease.work_id, lease.holder_id, lease.fencing_token,
                        lease.expires_at.isoformat(), lease.acquired_at.isoformat(),
                        lease.tenant_id,
                    ),
                )
        await asyncio.to_thread(_save)

    async def release_lease(self, work_id: str) -> None:
        def _rel():
            with self._get_connection() as conn:
                conn.execute("DELETE FROM orchestrator_lease WHERE work_id = ?", (work_id,))
        await asyncio.to_thread(_rel)

    async def list_active_leases(self, tenant_id: str = "default") -> List[Lease]:
        def _list():
            with self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM orchestrator_lease WHERE tenant_id = ?", (tenant_id,))
                res = []
                for row in cur.fetchall():
                    res.append(
                        Lease(
                            work_id=row["work_id"],
                            holder_id=row["holder_id"],
                            fencing_token=row["fencing_token"],
                            expires_at=parse_iso(row["lease_expires_at"]),
                            acquired_at=parse_iso(row["acquired_at"]),
                            tenant_id=row["tenant_id"],
                        )
                    )
                return res
        return await asyncio.to_thread(_list)

    async def next_fencing_token(self) -> int:
        def _next():
            with self._get_connection() as conn:
                conn.execute("UPDATE orchestrator_sequence SET val = val + 1 WHERE name = 'fencing';")
                cur = conn.execute("SELECT val FROM orchestrator_sequence WHERE name = 'fencing';")
                return cur.fetchone()[0]
        return await asyncio.to_thread(_next)

    async def append_event(self, event: DomainEvent) -> None:
        self._trigger_failpoint("before_append_event")
        def _append():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orchestrator_event (
                        event_id, work_id, execution_id, event_type, specversion,
                        source, sequence_number, data_json, correlation_id, causation_id, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id, event.work_id, event.execution_id, event.type,
                        event.specversion, event.source, event.sequence_number,
                        json.dumps(event.data), event.correlation_id, event.causation_id,
                        event.time.isoformat(),
                    ),
                )
        await asyncio.to_thread(_append)
        self._trigger_failpoint("after_append_event")

    async def list_events_for_work(self, work_id: str) -> List[DomainEvent]:
        def _list():
            with self._get_connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM orchestrator_event WHERE work_id = ? ORDER BY sequence_number ASC",
                    (work_id,),
                )
                res = []
                for row in cur.fetchall():
                    res.append(
                        DomainEvent(
                            id=row["event_id"],
                            work_id=row["work_id"],
                            execution_id=row["execution_id"],
                            type=row["event_type"],
                            specversion=row["specversion"],
                            source=row["source"],
                            sequence_number=row["sequence_number"],
                            data=json.loads(row["data_json"]),
                            correlation_id=row["correlation_id"],
                            causation_id=row["causation_id"],
                            time=parse_iso(row["occurred_at"]),
                        )
                    )
                return res
        return await asyncio.to_thread(_list)

    async def fetch_pending_outbox(self, limit: int = 100) -> List[DomainEvent]:
        def _fetch():
            with self._get_connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM orchestrator_event WHERE applied_at IS NULL ORDER BY occurred_at ASC LIMIT ?",
                    (limit,),
                )
                res = []
                for row in cur.fetchall():
                    res.append(
                        DomainEvent(
                            id=row["event_id"],
                            work_id=row["work_id"],
                            execution_id=row["execution_id"],
                            type=row["event_type"],
                            specversion=row["specversion"],
                            source=row["source"],
                            sequence_number=row["sequence_number"],
                            data=json.loads(row["data_json"]),
                            correlation_id=row["correlation_id"],
                            causation_id=row["causation_id"],
                            time=parse_iso(row["occurred_at"]),
                        )
                    )
                return res
        return await asyncio.to_thread(_fetch)

    async def mark_outbox_applied(self, event_id: str) -> None:
        def _mark():
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE orchestrator_event SET applied_at = ? WHERE event_id = ?",
                    (datetime.now(timezone.utc).isoformat(), event_id),
                )
        await asyncio.to_thread(_mark)
