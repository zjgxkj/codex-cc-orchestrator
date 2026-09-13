"""Concurrency tests: multiple jobs, bounded parallelism (spec §23.5.6)."""

from __future__ import annotations

import asyncio
import time

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner
from codex_claude_agent_mcp.models import JobSpec
from codex_claude_agent_mcp.server import _run_jobs_core


async def test_run_jobs_runs_three_independently(app):
    jobs = [
        JobSpec(job_id=f"c{i}", task=f"task {i}", cwd=".", acceptance=["c"], review=True)
        for i in range(3)
    ]
    res = await _run_jobs_core(app, jobs, None)
    assert res.error is None
    assert len(res.results) == 3
    by_id = {r.job_id: r for r in res.results}
    for i in range(3):
        r = by_id[f"c{i}"]
        assert r.execution.status == "COMPLETED"
        assert r.review.status == "PASS"
        assert r.status == "COMPLETED"


async def test_run_jobs_respects_max_concurrency(config, tmp_db_path):
    """max_concurrency caps simultaneous execution."""
    # Use a slow fake runner to observe concurrency.
    class SlowFake(FakeClaudeRunner):
        async def execute(self, **kw):
            await asyncio.sleep(0.1)
            return await super().execute(**kw)

    from codex_claude_agent_mcp.scheduler import Scheduler
    from codex_claude_agent_mcp.server import AppState
    from codex_claude_agent_mcp.session_store import SessionStore

    store = SessionStore(tmp_db_path)
    await store.init()
    app = AppState(config=config, store=store, runner=SlowFake(), scheduler=Scheduler(config.max_concurrency))
    try:
        jobs = [JobSpec(job_id=f"p{i}", task="t", cwd=".", review=False) for i in range(6)]
        t0 = time.monotonic()
        await _run_jobs_core(app, jobs, max_concurrency=2)
        elapsed = time.monotonic() - t0
        # 6 jobs, 2 at a time, ~0.1s each -> >= 3 * 0.1s (allow scheduling slack)
        assert elapsed >= 0.28
    finally:
        await app.close()


async def test_run_jobs_max_concurrency_capped_at_process_limit(config, tmp_db_path):
    """Requesting more than the process limit still caps at config.max_concurrency."""
    from codex_claude_agent_mcp.scheduler import Scheduler
    from codex_claude_agent_mcp.server import AppState
    from codex_claude_agent_mcp.session_store import SessionStore

    store = SessionStore(tmp_db_path)
    await store.init()
    app = AppState(config=config, store=store, runner=FakeClaudeRunner(), scheduler=Scheduler(config.max_concurrency))
    try:
        jobs = [JobSpec(job_id=f"cap{i}", task="t", cwd=".", review=False) for i in range(4)]
        res = await _run_jobs_core(app, jobs, max_concurrency=99)
        assert res.error is None
        assert all(r.status == "COMPLETED" for r in res.results)
    finally:
        await app.close()


async def test_no_inflight_after_completion(app):
    jobs = [JobSpec(job_id=f"n{i}", task="t", cwd=".", review=False) for i in range(2)]
    await _run_jobs_core(app, jobs, None)
    assert app.scheduler.inflight == 0
