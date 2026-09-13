"""Lifecycle tests: execute -> review -> continue (spec §23.1, §23.5)."""

from __future__ import annotations

import asyncio

import pytest

from codex_claude_agent_mcp.errors import (
    JOB_CONTEXT_MISMATCH,
    JOB_STATE_CONFLICT,
    SESSION_MISMATCH,
    SESSION_NOT_FOUND,
)
from codex_claude_agent_mcp.server import (
    _continue_task_core,
    _execute_task_core,
    _review_task_core,
    _run_job_core,
)
from codex_claude_agent_mcp.models import JobSpec


async def test_execute_returns_session_id(app):
    res = await _execute_task_core(app, "l1", "do work", ".", ["c"], None, None, 60)
    assert res.status == "COMPLETED"
    assert res.execution_session_id is not None
    assert res.error is None


async def test_review_uses_different_session_than_execution(app):
    er = await _execute_task_core(app, "l2", "do work", ".", ["c"], None, None, 60)
    rv = await _review_task_core(app, "l2", "do work", ["c"], ".", er.summary, None, 60)
    assert rv.status == "PASS"
    assert rv.review_session_id != er.execution_session_id


async def test_continue_resumes_original_session(app):
    er = await _execute_task_core(app, "l3", "do work", ".", ["c"], None, None, 60)
    cr = await _continue_task_core(app, "l3", er.execution_session_id, "fix the bug", ".", None, None, 60)
    assert cr.status == "COMPLETED"
    # continue reuses the same execution session id
    assert cr.execution_session_id == er.execution_session_id
    # the fake runner recorded a continue call with that session id
    continue_calls = [c for c in app.runner.calls if c[0] == "continue"]  # type: ignore[attr-defined]
    assert continue_calls
    assert continue_calls[-1][1]["execution_session_id"] == er.execution_session_id


async def test_continue_unknown_job_returns_session_not_found(app):
    res = await _continue_task_core(app, "nope", "sid", "feedback", ".", None, None, 60)
    assert res.error is not None
    assert res.error.code == SESSION_NOT_FOUND


async def test_continue_missing_session_id(app):
    await _execute_task_core(app, "l4", "do work", ".", None, None, None, 60)
    res = await _continue_task_core(app, "l4", "", "feedback", ".", None, None, 60)
    assert res.error is not None
    assert res.error.code == "INVALID_ARGUMENT"


async def test_run_job_review_fail_review_failed_status(app):
    app.runner.review_mode = "review_fail"  # type: ignore[attr-defined]
    res = await _run_job_core(
        app,
        JobSpec(job_id="l5", task="do work", cwd=".", acceptance=["c"], review=True),
    )
    assert res.execution.status == "COMPLETED"
    assert res.review.status == "FAIL"
    assert res.status == "REVIEW_FAILED"
    assert res.review.unmet_criteria  # non-empty evidence of failure


async def test_run_job_skips_review_when_blocked(app):
    app.runner.execute_mode = "blocked"  # type: ignore[attr-defined]
    res = await _run_job_core(
        app,
        JobSpec(job_id="l6", task="do work", cwd=".", acceptance=["c"], review=True),
    )
    assert res.execution.status == "BLOCKED"
    assert res.review is None
    assert res.status == "BLOCKED"


async def test_review_fail_then_continue_flow(app):
    """Full review-fail -> continue flow (spec §8.3)."""
    app.runner.review_mode = "review_fail"  # type: ignore[attr-defined]
    res = await _run_job_core(
        app,
        JobSpec(job_id="l7", task="do work", cwd=".", acceptance=["c"], review=True),
    )
    assert res.status == "REVIEW_FAILED"
    # Sol decides to resume the original execution session with feedback
    cr = await _continue_task_core(
        app, "l7", res.execution.execution_session_id,
        "please address: " + "; ".join(res.review.unmet_criteria), ".", None, None, 60,
    )
    assert cr.status == "COMPLETED"


async def test_review_task_empty_acceptance_errors(app):
    res = await _review_task_core(app, "l8", "task", [], ".", None, None, 60)
    assert res.error is not None
    assert res.error.code == "REVIEW_REQUIRES_ACCEPTANCE"


async def test_review_rejects_task_or_acceptance_rebinding(app):
    er = await _execute_task_core(app, "bound-1", "task A", ".", ["criterion A"], None, None, 60)
    assert er.status == "COMPLETED"

    wrong_task = await _review_task_core(
        app, "bound-1", "task B", ["criterion A"], ".", er.summary, None, 60,
    )
    assert wrong_task.error is not None
    assert wrong_task.error.code == JOB_CONTEXT_MISMATCH
    assert wrong_task.error.details["mismatched_fields"] == ["original_task"]

    wrong_acceptance = await _review_task_core(
        app, "bound-1", "task A", ["criterion B"], ".", er.summary, None, 60,
    )
    assert wrong_acceptance.error is not None
    assert wrong_acceptance.error.code == JOB_CONTEXT_MISMATCH
    assert wrong_acceptance.error.details["mismatched_fields"] == ["acceptance"]
    assert not [call for call in app.runner.calls if call[0] == "review"]  # type: ignore[attr-defined]


async def test_review_rejects_cwd_rebinding(app, tmp_path):
    er = await _execute_task_core(app, "bound-cwd", "task", ".", ["criterion"], None, None, 60)
    other = tmp_path / "other-project"
    other.mkdir()
    res = await _review_task_core(
        app, "bound-cwd", "task", ["criterion"], str(other), er.summary, None, 60,
    )
    assert res.error is not None
    assert res.error.code == JOB_CONTEXT_MISMATCH
    assert res.error.details["mismatched_fields"] == ["cwd"]


async def test_continue_rejects_session_from_another_job(app):
    first = await _execute_task_core(app, "session-a", "task", ".", [], None, None, 60)
    second = await _execute_task_core(app, "session-b", "task", ".", [], None, None, 60)
    res = await _continue_task_core(
        app, "session-a", second.execution_session_id, "feedback", ".", None, None, 60,
    )
    assert res.error is not None
    assert res.error.code == SESSION_MISMATCH
    stored = await app.store.get_job("session-a")
    assert stored["execution_session_id"] == first.execution_session_id
    assert not [call for call in app.runner.calls if call[0] == "continue"]  # type: ignore[attr-defined]


async def test_busy_job_cannot_be_reviewed_concurrently(app):
    app.runner.execute_mode = "slow"  # type: ignore[attr-defined]
    task = asyncio.create_task(
        _execute_task_core(app, "busy-1", "task", ".", ["criterion"], None, None, 60)
    )
    await asyncio.sleep(0.1)
    review = await _review_task_core(
        app, "busy-1", "task", ["criterion"], ".", None, None, 60,
    )
    assert review.error is not None
    assert review.error.code == JOB_STATE_CONFLICT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_execute_cancelled_marks_job_cancelled(app):
    app.runner.execute_mode = "slow"  # type: ignore[attr-defined]
    task = asyncio.create_task(
        _execute_task_core(app, "cancel-exec", "task", ".", [], None, None, 60)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await app.store.get_job("cancel-exec")
    assert stored["status"] == "EXECUTION_INCOMPLETE"
    assert stored["execution_status"] == "INCOMPLETE"


async def test_review_cancelled_marks_job_cancelled(app):
    app.runner.review_mode = "slow"  # type: ignore[attr-defined]
    task = asyncio.create_task(
        _review_task_core(app, "cancel-review", "task", ["criterion"], ".", None, None, 60)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await app.store.get_job("cancel-review")
    assert stored["status"] == "REVIEW_INCOMPLETE"
    assert stored["review_status"] == "INCOMPLETE"


async def test_continue_cancelled_marks_job_cancelled(app):
    er = await _execute_task_core(app, "cancel-continue", "task", ".", [], None, None, 60)
    app.runner.execute_mode = "slow"  # type: ignore[attr-defined]
    task = asyncio.create_task(_continue_task_core(
        app, "cancel-continue", er.execution_session_id, "feedback", ".", None, None, 60,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await app.store.get_job("cancel-continue")
    assert stored["status"] == "EXECUTION_INCOMPLETE"
    assert stored["execution_status"] == "INCOMPLETE"
