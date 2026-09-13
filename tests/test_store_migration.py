"""Backward-compatible SQLite migrations for the ``jobs`` table.

A database created by an older server version lacks ``project_root`` and the
per-stage ``execution_status``/``review_status`` columns. Opening it must not
fail, the columns must be added additively, and pre-existing rows must keep the
legacy ALLOWED_PROJECT_ROOTS authorization basis (NULL root) and NULL stage
statuses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_claude_agent_mcp.errors import MCPError
from codex_claude_agent_mcp.session_store import SessionStore

_LEGACY_SCHEMA = """
CREATE TABLE jobs (
    job_id                TEXT    PRIMARY KEY,
    task                  TEXT    NOT NULL,
    acceptance_json       TEXT    NOT NULL DEFAULT '[]',
    cwd                   TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    execution_session_id  TEXT,
    review_session_id     TEXT,
    execution_summary     TEXT,
    review_summary        TEXT,
    created_at            REAL    NOT NULL,
    updated_at            REAL    NOT NULL
);
"""


def _create_legacy_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            """INSERT INTO jobs (job_id, task, acceptance_json, cwd, status,
                                 execution_session_id, review_session_id,
                                 created_at, updated_at)
               VALUES ('legacy-1', 'old task', '["c"]', 'D:/old/project', 'COMPLETED',
                       'legacy-exec', 'legacy-review', 1.0, 2.0);"""
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(jobs);")}
    finally:
        conn.close()


async def test_legacy_db_is_migrated_and_readable(tmp_db_path):
    _create_legacy_db(tmp_db_path)
    assert "project_root" not in _columns(tmp_db_path)
    assert "execution_status" not in _columns(tmp_db_path)
    assert "review_status" not in _columns(tmp_db_path)

    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        assert "project_root" in _columns(tmp_db_path)
        assert "execution_status" in _columns(tmp_db_path)
        assert "review_status" in _columns(tmp_db_path)
        assert {
            "execution_session_confirmed", "review_session_confirmed",
            "execution_error_json", "review_error_json",
        } <= _columns(tmp_db_path)
        job = await store.get_job("legacy-1")
        assert job is not None
        assert job["task"] == "old task"
        assert job["acceptance"] == ["c"]
        assert job["project_root"] is None  # legacy permanent-roots basis preserved
        assert job["execution_status"] is None  # legacy rows keep NULL stage statuses
        assert job["review_status"] is None
        assert job["execution_session_confirmed"] is True
        assert job["review_session_confirmed"] is True
        assert job["execution_error"] is None and job["review_error"] is None
        assert (await store.list_jobs())[0]["project_root"] is None
        assert {"owner_id", "execution_result_json", "review_result_json"} <= _columns(tmp_db_path)
        assert job["owner_id"] is None
        assert job["execution_result"] is None and job["review_result"] is None
        assert (await store.get_usage("legacy-1")).total.calls == 0
        assert (await store.get_usage("legacy-1")).total.input_tokens is None
        assert await store.get_invocations("legacy-1") == []
    finally:
        await store.close()


async def test_migration_is_idempotent_across_reopens(tmp_db_path):
    _create_legacy_db(tmp_db_path)
    first = SessionStore(tmp_db_path)
    await first.init()
    await first.close()
    second = SessionStore(tmp_db_path)
    await second.init()
    try:
        assert {"project_root", "execution_status", "review_status"} <= _columns(tmp_db_path)
        await second.create_job("new-1", "t", [], "D:/x", "EXECUTING", project_root="D:/x")
        assert (await second.get_job("new-1"))["project_root"] == "D:/x"
        assert (await second.get_job("legacy-1"))["project_root"] is None
        assert (await second.get_job("legacy-1"))["execution_session_confirmed"] is True
    finally:
        await second.close()


async def test_fresh_db_has_project_root_column(tmp_db_path):
    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        assert "project_root" in _columns(tmp_db_path)
        assert {"execution_status", "review_status"} <= _columns(tmp_db_path)
        created = await store.create_job("fresh-1", "t", ["c"], "D:/proj", "QUEUED", project_root="D:/proj")
        assert created["project_root"] == "D:/proj"
        assert created["execution_status"] is None
        assert created["execution_session_confirmed"] is False
        assert (await store.get_job("fresh-1"))["project_root"] == "D:/proj"
    finally:
        await store.close()


async def test_stage_status_columns_persist_per_stage(tmp_db_path):
    """Stage statuses record per-stage verdicts alongside the job-level status."""
    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        await store.create_job("stage-1", "t", ["c"], "D:/p", "EXECUTING")
        await store.set_execution_session("stage-1", "sid-1", "COMPLETED", "done")
        job = await store.get_job("stage-1")
        assert job["execution_status"] == "COMPLETED"
        assert job["status"] == "COMPLETED"

        await store.set_review_session("stage-1", "sid-2", "REVIEW_INCOMPLETE", None, review_status="INCOMPLETE")
        job = await store.get_job("stage-1")
        assert job["review_status"] == "INCOMPLETE"
        assert job["status"] == "REVIEW_INCOMPLETE"
        assert job["execution_status"] == "COMPLETED"  # stage statuses are independent
    finally:
        await store.close()


async def test_preallocated_unconfirmed_id_stays_unconfirmed_after_reopen(tmp_db_path):
    first = SessionStore(tmp_db_path)
    await first.init()
    await first.create_job(
        "prealloc-1", "t", [], "D:/p", "EXECUTING",
        execution_session_id="preallocated-but-unseen",
    )
    await first.close()

    second = SessionStore(tmp_db_path)
    await second.init()
    try:
        job = await second.get_job("prealloc-1")
        assert job["execution_session_id"] == "preallocated-but-unseen"
        assert job["execution_session_confirmed"] is False
    finally:
        await second.close()


async def test_project_root_is_not_mutable(tmp_db_path):
    """A persisted job can never switch its authorization root."""
    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        await store.create_job("imm-1", "t", [], "D:/proj", "EXECUTING", project_root="D:/proj")
        with pytest.raises(MCPError) as exc:
            await store.update_fields("imm-1", project_root="D:/elsewhere")
        assert exc.value.code == "STATE_STORE_ERROR"
        assert (await store.get_job("imm-1"))["project_root"] == "D:/proj"
    finally:
        await store.close()
