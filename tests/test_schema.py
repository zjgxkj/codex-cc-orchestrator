"""Schema and input-validation tests (spec §23.1)."""

from __future__ import annotations

import pytest

from codex_claude_agent_mcp.config import Config
from codex_claude_agent_mcp.errors import (
    INVALID_ARGUMENT,
    INVALID_CWD,
    MCPError,
    REVIEW_REQUIRES_ACCEPTANCE,
)
from codex_claude_agent_mcp.models import JobSpec
from codex_claude_agent_mcp.server import _run_job_core, _run_jobs_core


async def test_review_requires_acceptance(app):
    """review=true + empty acceptance -> REVIEW_REQUIRES_ACCEPTANCE, before any Claude call."""
    spec = JobSpec(job_id="j1", task="do thing", cwd=".", acceptance=[], review=True)
    res = await _run_job_core(app, spec)
    assert res.execution.error is not None
    assert res.execution.error.code == REVIEW_REQUIRES_ACCEPTANCE
    # No Claude call should have happened.
    assert app.runner.calls == []  # type: ignore[attr-defined]


async def test_review_false_no_acceptance_is_valid(app):
    """review=false + empty acceptance -> valid, executes only."""
    spec = JobSpec(job_id="j2", task="do thing", cwd=".", acceptance=[], review=False)
    res = await _run_job_core(app, spec)
    assert res.execution.error is None
    assert res.execution.status == "COMPLETED"
    assert res.review is None


async def test_invalid_cwd_rejected(app):
    app.config.allow_any_cwd = False
    app.config.allowed_roots = [__import__("pathlib").Path("C:/safe").resolve()]
    res = await _run_job_core(
        app,
        JobSpec(job_id="j3", task="t", cwd="C:/nope/../../escape", acceptance=[], review=False),
    )
    assert res.execution.error is not None
    assert res.execution.error.code == INVALID_CWD


async def test_invalid_effort(app):
    from codex_claude_agent_mcp.server import _execute_task_core
    res = await _execute_task_core(app, "j4", "t", ".", None, None, "extreme", 60)
    assert res.error is not None
    assert res.error.code == INVALID_ARGUMENT


async def test_duplicate_job_id(app):
    from codex_claude_agent_mcp.server import _execute_task_core
    await _execute_task_core(app, "dup", "t", ".", None, None, None, 60)
    res = await _execute_task_core(app, "dup", "t", ".", None, None, None, 60)
    assert res.error is not None
    assert res.error.code == "STATE_STORE_ERROR"


async def test_run_jobs_duplicate_job_id_in_batch(app):
    jobs = [
        JobSpec(job_id="x", task="t", cwd=".", acceptance=[], review=False),
        JobSpec(job_id="x", task="t", cwd=".", acceptance=[], review=False),
    ]
    res = await _run_jobs_core(app, jobs, None)
    assert res.error is not None
    assert res.error.code == INVALID_ARGUMENT


async def test_run_jobs_per_job_review_error(app):
    """A bad job (review+empty acceptance) gets an inline error; siblings still run."""
    jobs = [
        JobSpec(job_id="bad", task="t", cwd=".", acceptance=[], review=True),
        JobSpec(job_id="good", task="t", cwd=".", acceptance=["c"], review=True),
    ]
    res = await _run_jobs_core(app, jobs, None)
    assert res.error is None
    by_id = {r.job_id: r for r in res.results}
    assert by_id["bad"].execution.error.code == REVIEW_REQUIRES_ACCEPTANCE
    assert by_id["good"].execution.status == "COMPLETED"
    assert by_id["good"].review.status == "PASS"


def test_config_validate_cwd_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    cfg = Config(
        max_concurrency=1, default_task_timeout_sec=60, max_task_timeout_sec=120,
        db_path=tmp_path / "db", cli_path=None, allowed_roots=[root.resolve()],
        allowed_models=[], allow_any_cwd=False,
    )
    # allowed
    p = cfg.validate_cwd(str(root))
    assert p == root.resolve()
    # disallowed
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(MCPError) as exc:
        cfg.validate_cwd(str(other))
    assert exc.value.code == INVALID_CWD


def test_config_from_env_allows_empty_allowed_roots(monkeypatch, tmp_path):
    """ALLOWED_PROJECT_ROOTS is an optional long-term trust list, not a gate."""
    monkeypatch.delenv("ALLOWED_PROJECT_ROOTS", raising=False)
    monkeypatch.setenv("CODEX_CLAUDE_AGENT_MCP_DB_PATH", str(tmp_path / "state.db"))
    cfg = Config.from_env()
    assert cfg.allowed_roots == []
    assert cfg.allow_any_cwd is False


def test_config_rejects_relative_cwd_when_roots_enforced(tmp_path):
    cfg = Config(
        max_concurrency=1, default_task_timeout_sec=60, max_task_timeout_sec=120,
        db_path=tmp_path / "db", cli_path=None, allowed_roots=[tmp_path.resolve()],
        allowed_models=[], allow_any_cwd=False,
    )
    with pytest.raises(MCPError) as exc:
        cfg.validate_cwd(".")
    assert exc.value.code == INVALID_CWD


def test_config_rejects_missing_cwd(tmp_path):
    cfg = Config(
        max_concurrency=1, default_task_timeout_sec=60, max_task_timeout_sec=120,
        db_path=tmp_path / "db", cli_path=None, allowed_roots=[tmp_path.resolve()],
        allowed_models=[], allow_any_cwd=False,
    )
    with pytest.raises(MCPError) as exc:
        cfg.validate_cwd(str(tmp_path / "does-not-exist"))
    assert exc.value.code == INVALID_CWD
