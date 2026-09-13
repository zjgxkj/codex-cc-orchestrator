"""SQLite-backed persistent state for jobs and Claude sessions.

Design goals (spec §15):

* Survives MCP server process exit so sessions can be resumed later.
* Tolerates **multiple MCP server processes** touching the same DB file at
  once (different Codex threads each spawn their own STDIO subprocess).

Multi-process safety is achieved with:

* ``PRAGMA journal_mode=WAL`` — concurrent readers + one writer.
* ``PRAGMA busy_timeout`` — wait instead of raising SQLITE_BUSY on contention.
* ``BEGIN IMMEDIATE`` for writes — acquire the write lock up front.
* ``UNIQUE`` on ``job_id`` — duplicate job submission is a structured error.

All access is funneled through an :class:`asyncio.Lock` *within* one process,
so the single sqlite3 connection is never used concurrently on the event loop.
Cross-process coordination is handled by SQLite itself.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .errors import MCPError, SESSION_MISMATCH, STATE_STORE_ERROR
from .logging import get_logger

log = get_logger("session_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id                TEXT    PRIMARY KEY,
    task                  TEXT    NOT NULL,
    acceptance_json       TEXT    NOT NULL DEFAULT '[]',
    cwd                   TEXT    NOT NULL,
    project_root          TEXT,
    status                TEXT    NOT NULL,
    execution_status      TEXT,
    review_status         TEXT,
    execution_session_id  TEXT,
    execution_session_confirmed INTEGER NOT NULL DEFAULT 0,
    review_session_id     TEXT,
    review_session_confirmed INTEGER NOT NULL DEFAULT 0,
    execution_summary     TEXT,
    review_summary        TEXT,
    execution_error_json  TEXT,
    review_error_json     TEXT,
    created_at            REAL    NOT NULL,
    updated_at            REAL    NOT NULL
);
"""

# Additive, backward-compatible migrations for databases created by an older
# server version. ``project_root`` stays NULL for pre-existing jobs, which keeps
# them on the legacy ALLOWED_PROJECT_ROOTS authorization basis; a job never
# switches root afterwards (the column is not writable via ``update_fields``).
# Stage statuses separate completed verdicts from INCOMPLETE transport/protocol
# outcomes and are recorded alongside the aggregate job status.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("project_root", "ALTER TABLE jobs ADD COLUMN project_root TEXT;"),
    ("execution_status", "ALTER TABLE jobs ADD COLUMN execution_status TEXT;"),
    ("review_status", "ALTER TABLE jobs ADD COLUMN review_status TEXT;"),
    ("execution_session_confirmed", "ALTER TABLE jobs ADD COLUMN execution_session_confirmed INTEGER NOT NULL DEFAULT 0;"),
    ("review_session_confirmed", "ALTER TABLE jobs ADD COLUMN review_session_confirmed INTEGER NOT NULL DEFAULT 0;"),
    ("execution_error_json", "ALTER TABLE jobs ADD COLUMN execution_error_json TEXT;"),
    ("review_error_json", "ALTER TABLE jobs ADD COLUMN review_error_json TEXT;"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs);")}
    added_execution_confirmation = "execution_session_confirmed" not in columns
    added_review_confirmation = "review_session_confirmed" not in columns
    for column, ddl in _MIGRATIONS:
        if column not in columns:
            conn.execute(ddl)
            log.info("session store migration: added jobs.%s", column)
    # Legacy ids were written only after the SDK returned them, so they are
    # safe to mark confirmed. Newly preallocated ids remain unconfirmed until
    # the stream echoes them.
    if added_execution_confirmation:
        conn.execute(
            "UPDATE jobs SET execution_session_confirmed = 1 "
            "WHERE execution_session_id IS NOT NULL AND execution_session_id <> '';"
        )
    if added_review_confirmation:
        conn.execute(
            "UPDATE jobs SET review_session_confirmed = 1 "
            "WHERE review_session_id IS NOT NULL AND review_session_id <> '';"
        )


class SessionStore:
    """Async wrapper around a multi-process-safe SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    async def init(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,           # busy_timeout equivalent
                isolation_level=None,   # autocommit; we manage txns manually
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row  # name-keyed rows, immune to column order
            conn.executescript(_SCHEMA)
            _apply_migrations(conn)
            return conn

        async with self._lock:
            self._conn = await asyncio.to_thread(_open)
        log.info("session store ready: %s", self.db_path)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise MCPError(STATE_STORE_ERROR, "session store not initialized")
        return self._conn

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        # sqlite3.Row maps by column *name*, so future schema migrations that
        # add/reorder columns cannot silently mis-map fields (the old positional
        # zip against a hardcoded tuple would truncate at the shortest side).
        d = dict(row)
        d["acceptance"] = json.loads(d.pop("acceptance_json"))
        for field in ("execution_error_json", "review_error_json"):
            raw = d.pop(field, None)
            d[field.removesuffix("_json")] = json.loads(raw) if raw else None
        d["execution_session_confirmed"] = bool(d.get("execution_session_confirmed"))
        d["review_session_confirmed"] = bool(d.get("review_session_confirmed"))
        return d

    # -- operations ----------------------------------------------------------

    async def create_job(
        self,
        job_id: str,
        task: str,
        acceptance: list[str],
        cwd: str,
        status: str = "QUEUED",
        project_root: str | None = None,
        execution_status: str | None = None,
        review_status: str | None = None,
        execution_session_id: str | None = None,
        review_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new job. Raises MCPError(STATE_STORE_ERROR) on duplicate job_id.

        ``project_root`` is the resolved job-scoped authorization root supplied by
        the caller; ``None`` means the legacy ALLOWED_PROJECT_ROOTS basis. It is
        persisted for the life of the job and is intentionally not updatable.
        """
        now = self._now()

        def _do() -> dict[str, Any]:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """INSERT INTO jobs
                       (job_id, task, acceptance_json, cwd, project_root, status,
                        execution_status, review_status,
                        execution_session_id, review_session_id,
                        execution_summary, review_summary,
                        execution_error_json, review_error_json,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?);""",
                    (
                        job_id, task, json.dumps(acceptance), cwd, project_root,
                        status, execution_status, review_status,
                        execution_session_id, review_session_id, now, now,
                    ),
                )
                conn.execute("COMMIT;")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK;")
                raise MCPError(
                    STATE_STORE_ERROR,
                    f"duplicate job_id: {job_id}",
                    retryable=False,
                    details={"job_id": job_id},
                ) from exc
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise MCPError(STATE_STORE_ERROR, f"create_job failed: {exc}") from exc
            return {
                "job_id": job_id, "task": task, "acceptance": acceptance,
                "cwd": cwd, "project_root": project_root, "status": status,
                "execution_status": execution_status, "review_status": review_status,
                "execution_session_id": execution_session_id,
                "execution_session_confirmed": False,
                "review_session_id": review_session_id,
                "review_session_confirmed": False,
                "execution_summary": None, "review_summary": None,
                "execution_error": None, "review_error": None,
                "created_at": now, "updated_at": now,
            }

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        def _do() -> dict[str, Any] | None:
            cur = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?;", (job_id,))
            return self._row_to_dict(cur.fetchone())

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def update_fields(self, job_id: str, **fields: Any) -> None:
        """Update arbitrary columns and bump updated_at.

        ``task``, ``acceptance_json``, ``cwd`` and ``project_root`` are job
        identity: they are deliberately not updatable, so a persisted job can
        never switch its task package or its authorization root.
        """
        if not fields:
            return
        allowed = {
            "status", "execution_status", "review_status",
            "execution_session_id", "review_session_id",
            "execution_session_confirmed", "review_session_confirmed",
            "execution_summary", "review_summary",
            "execution_error_json", "review_error_json",
        }
        bad = set(fields) - allowed
        if bad:
            raise MCPError(STATE_STORE_ERROR, f"cannot update fields: {bad}")
        now = self._now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        params: list[Any] = [*fields.values(), now, job_id]

        def _do() -> None:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    f"UPDATE jobs SET {cols}, updated_at = ? WHERE job_id = ?;",
                    params,
                )
                conn.execute("COMMIT;")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise MCPError(STATE_STORE_ERROR, f"update_fields failed: {exc}") from exc

        async with self._lock:
            await asyncio.to_thread(_do)

    async def transition_status(
        self,
        job_id: str,
        *,
        from_statuses: set[str] | frozenset[str],
        to_status: str,
    ) -> bool:
        """Atomically claim a job state transition.

        The compare-and-set happens in SQLite, so it also protects against two
        independent STDIO server processes trying to review/resume the same job
        at once.  ``False`` means the job was missing or no longer had one of
        the expected source states.
        """
        if not from_statuses:
            raise MCPError(STATE_STORE_ERROR, "from_statuses must not be empty")
        now = self._now()
        expected = sorted(from_statuses)
        placeholders = ", ".join("?" for _ in expected)

        def _do() -> bool:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.execute(
                    f"""UPDATE jobs
                        SET status = ?, updated_at = ?
                        WHERE job_id = ? AND status IN ({placeholders});""",
                    (to_status, now, job_id, *expected),
                )
                conn.execute("COMMIT;")
                return cur.rowcount == 1
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise MCPError(STATE_STORE_ERROR, f"transition_status failed: {exc}") from exc

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def list_jobs(self) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            cur = self.conn.execute("SELECT * FROM jobs ORDER BY created_at ASC;")
            return [r for r in (self._row_to_dict(row) for row in cur.fetchall()) if r]

        async with self._lock:
            return await asyncio.to_thread(_do)

    # -- convenience ---------------------------------------------------------

    async def confirm_session(self, job_id: str, kind: str, session_id: str) -> None:
        """Confirm that the stream echoed this job's preallocated session id."""
        if kind not in {"execution", "review"}:
            raise MCPError(STATE_STORE_ERROR, f"invalid session kind: {kind}")
        id_field = f"{kind}_session_id"
        confirmed_field = f"{kind}_session_confirmed"
        now = self._now()

        def _do() -> None:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.execute(
                    f"""UPDATE jobs SET {confirmed_field} = 1, updated_at = ?
                        WHERE job_id = ? AND {id_field} = ?;""",
                    (now, job_id, session_id),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK;")
                    raise MCPError(
                        SESSION_MISMATCH,
                        f"observed {kind} session does not match the persisted job",
                        details={"job_id": job_id, "session_id": session_id},
                    )
                conn.execute("COMMIT;")
            except MCPError:
                raise
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise MCPError(STATE_STORE_ERROR, f"confirm_session failed: {exc}") from exc

        async with self._lock:
            await asyncio.to_thread(_do)

    async def set_execution_session(
        self, job_id: str, session_id: str | None, status: str,
        summary: str | None, *, execution_status: str | None = None,
        session_confirmed: bool | None = None, error: dict[str, Any] | None = None,
    ) -> None:
        """Persist the execution stage outcome: session id, stage and job status, summary."""
        fields: dict[str, Any] = {
            "status": status,
            "execution_status": execution_status or status,
            "execution_summary": summary,
            "execution_error_json": json.dumps(error) if error else None,
        }
        if session_id:
            fields["execution_session_id"] = session_id
        if session_confirmed is not None:
            fields["execution_session_confirmed"] = int(session_confirmed)
        await self.update_fields(job_id, **fields)

    async def set_review_session(
        self, job_id: str, session_id: str | None, status: str, summary: str | None,
        review_status: str | None = None, *, session_confirmed: bool | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Persist the review stage outcome.

        ``review_status`` is PASS/FAIL/INCOMPLETE; ``status`` is
        the job-level mapping (COMPLETED/REVIEW_FAILED/REVIEW_INCOMPLETE/...).
        """
        fields: dict[str, Any] = {
            "status": status,
            "review_status": review_status,
            "review_summary": summary,
            "review_error_json": json.dumps(error) if error else None,
        }
        if session_id:
            fields["review_session_id"] = session_id
        if session_confirmed is not None:
            fields["review_session_confirmed"] = int(session_confirmed)
        await self.update_fields(job_id, **fields)
