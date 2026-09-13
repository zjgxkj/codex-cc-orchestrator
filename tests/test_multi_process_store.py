"""Multi-process SQLite store tests (spec §23.4).

Two independent SessionStore instances on the same DB file must coexist without
corrupting state; duplicate job_id must be handled deterministically; one
process crashing must not break the other.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_claude_agent_mcp.errors import MCPError
from codex_claude_agent_mcp.session_store import SessionStore


async def test_two_stores_same_db_coexist(tmp_db_path):
    a = SessionStore(tmp_db_path)
    b = SessionStore(tmp_db_path)
    await a.init()
    await b.init()
    try:
        await a.create_job("job-a", "task A", ["c"], "/tmp", "EXECUTING")
        await b.create_job("job-b", "task B", ["c"], "/tmp", "EXECUTING")

        # each sees both (shared file)
        a_jobs = {j["job_id"] for j in await a.list_jobs()}
        b_jobs = {j["job_id"] for j in await b.list_jobs()}
        assert a_jobs == {"job-a", "job-b"}
        assert b_jobs == {"job-a", "job-b"}

        # b can update a's job
        await b.update_fields("job-a", status="COMPLETED", execution_session_id="sid-1")
        ja = await a.get_job("job-a")
        assert ja["status"] == "COMPLETED"
        assert ja["execution_session_id"] == "sid-1"
    finally:
        await a.close()
        await b.close()


async def test_duplicate_job_id_across_processes(tmp_db_path):
    a = SessionStore(tmp_db_path)
    b = SessionStore(tmp_db_path)
    await a.init()
    await b.init()
    try:
        await a.create_job("dup", "task", ["c"], "/tmp", "EXECUTING")
        with pytest.raises(MCPError) as exc:
            await b.create_job("dup", "task", ["c"], "/tmp", "EXECUTING")
        assert exc.value.code == "STATE_STORE_ERROR"
        assert "duplicate" in exc.value.message.lower()
    finally:
        await a.close()
        await b.close()


async def test_concurrent_writes_no_corruption(tmp_db_path):
    """Many stores writing distinct keys must all land without corruption."""
    stores = [SessionStore(tmp_db_path) for _ in range(4)]
    for s in stores:
        await s.init()
    try:
        async def write(i):
            await stores[i % 4].create_job(f"k{i}", f"task {i}", [], "/tmp", "EXECUTING")

        await asyncio.gather(*(write(i) for i in range(20)))
        all_jobs = await stores[0].list_jobs()
        ids = {j["job_id"] for j in all_jobs}
        assert ids == {f"k{i}" for i in range(20)}
    finally:
        for s in stores:
            await s.close()


async def test_one_store_close_does_not_break_other(tmp_db_path):
    a = SessionStore(tmp_db_path)
    b = SessionStore(tmp_db_path)
    await a.init()
    await b.init()
    await a.create_job("j1", "t", [], "/tmp", "EXECUTING")
    await a.close()  # "crash" one process
    # b still works
    await b.create_job("j2", "t", [], "/tmp", "EXECUTING")
    jb = await b.get_job("j2")
    assert jb is not None and jb["job_id"] == "j2"
    await b.close()


def test_multi_process_via_subprocesses(tmp_db_path):
    """Spawn two real subprocesses hitting the same DB (spec §23.4 integration)."""
    import subprocess
    import sys

    code = """
import asyncio, sys
from codex_claude_agent_mcp.session_store import SessionStore
async def main(path, job_id):
    s = SessionStore(path)
    await s.init()
    await s.create_job(job_id, "task", ["c"], "/tmp", "EXECUTING")
    jobs = {j["job_id"] for j in await s.list_jobs()}
    print("OK", job_id, sorted(jobs), flush=True)
    await s.close()
asyncio.run(main(sys.argv[1], sys.argv[2]))
"""
    procs = [
        subprocess.run([sys.executable, "-c", code, str(tmp_db_path), f"proc-{i}"],
                       capture_output=True, text=True, timeout=30)
        for i in range(2)
    ]
    for p in procs:
        assert p.returncode == 0, f"stderr: {p.stderr}"
        assert "OK" in p.stdout
