"""Scheduler & state-transition tests (spec §23.1, §23.3)."""

from __future__ import annotations

import asyncio

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner
from codex_claude_agent_mcp.models import JobSpec
from codex_claude_agent_mcp.scheduler import Scheduler
from codex_claude_agent_mcp.server import _run_job_core


async def test_per_process_semaphore_bounds_concurrency():
    """At most ``max_concurrency`` coroutines run at once; extras queue."""
    sched = Scheduler(max_concurrency=2)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def work():
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1

    await asyncio.gather(*(sched.run(work, label=f"t{i}") for i in range(6)))
    assert peak == 2


async def test_per_process_semaphore_is_not_global():
    """Two Scheduler instances are independent (cross-process limitation)."""
    a = Scheduler(2)
    b = Scheduler(2)
    assert a.max_concurrency == 2
    assert b.max_concurrency == 2
    # independent semaphores
    await a._sem.acquire()
    assert a._sem._value == 1  # type: ignore[attr-defined]
    assert b._sem._value == 2  # type: ignore[attr-defined]
    a._sem.release()


async def test_scheduler_continues_after_one_job_fails(app):
    """A worker failure must not break the scheduler (spec: service stays alive)."""
    app.runner.execute_mode = "failure"  # type: ignore[attr-defined]
    res1 = await _run_job_core(app, JobSpec(job_id="f1", task="t", cwd=".", review=False), )
    assert res1.execution.status == "FAILED"
    # scheduler still works for next job
    app.runner.execute_mode = "success"  # type: ignore[attr-defined]
    res2 = await _run_job_core(app, JobSpec(job_id="f2", task="t", cwd=".", review=False))
    assert res2.execution.status == "COMPLETED"


async def test_state_transitions_persisted(app):
    from codex_claude_agent_mcp.server import _execute_task_core, _review_task_core

    er = await _execute_task_core(app, "st1", "task", ".", ["c"], None, None, 60)
    assert er.status == "COMPLETED"
    job = await app.store.get_job("st1")
    assert job is not None
    assert job["status"] == "COMPLETED"
    assert job["execution_session_id"] == er.execution_session_id

    rv = await _review_task_core(app, "st1", "task", ["c"], ".", er.summary, None, 60)
    assert rv.status == "PASS"
    job = await app.store.get_job("st1")
    assert job["status"] == "COMPLETED"
    assert job["review_session_id"] == rv.review_session_id
    assert job["review_session_id"] != job["execution_session_id"]


async def test_blocked_transition(app):
    app.runner.execute_mode = "blocked"  # type: ignore[attr-defined]
    from codex_claude_agent_mcp.server import _execute_task_core
    res = await _execute_task_core(app, "b1", "t", ".", None, None, None, 60)
    assert res.status == "BLOCKED"
    job = await app.store.get_job("b1")
    assert job["status"] == "BLOCKED"
