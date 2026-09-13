"""Job-scoped temporary project authorization (``project_root``).

Covers the explicit ``project_root`` supplied by Codex: it authorizes a single
job's ``cwd`` even outside ``ALLOWED_PROJECT_ROOTS``, is persisted with the job,
and can never be widened or switched later by ``continue_task`` / ``review_task``.

All tests are deterministic: they use ``FakeClaudeRunner`` (or a real runner with
its ``_run_query`` seam replaced) and never call the Claude API.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner, RealClaudeRunner
from codex_claude_agent_mcp.config import Config
from codex_claude_agent_mcp.errors import (
    INVALID_CWD,
    JOB_CONTEXT_MISMATCH,
    MCPError,
)
from codex_claude_agent_mcp.models import ContinueTaskInput, JobSpec
from codex_claude_agent_mcp.scheduler import Scheduler
from codex_claude_agent_mcp.server import (
    AppState,
    _continue_task_core,
    _execute_task_core,
    _review_task_core,
    _run_job_core,
    _run_jobs_core,
)
from codex_claude_agent_mcp.session_store import SessionStore


# --- fixtures / helpers -------------------------------------------------------

def _config(tmp_db_path: Path, *, allowed_roots: list[Path] | None = None) -> Config:
    """Production-shaped config: roots are optional and never bypass validation."""
    return Config(
        max_concurrency=2,
        default_task_timeout_sec=60,
        max_task_timeout_sec=120,
        db_path=tmp_db_path,
        cli_path=None,
        allowed_roots=[r.resolve() for r in (allowed_roots or [])],
        allowed_models=[],
        allow_any_cwd=False,
    )


class SpyRunner(FakeClaudeRunner):
    """FakeClaudeRunner that also records the cwd each Claude call was given."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cwds: list[tuple[str, str]] = []

    async def execute(self, *, cwd, **kw):
        self.cwds.append(("execute", cwd))
        return await super().execute(cwd=cwd, **kw)

    async def continue_session(self, *, cwd, **kw):
        self.cwds.append(("continue", cwd))
        return await super().continue_session(cwd=cwd, **kw)

    async def review(self, *, cwd, **kw):
        self.cwds.append(("review", cwd))
        return await super().review(cwd=cwd, **kw)


async def _make_app(config: Config, runner: Any | None = None) -> AppState:
    store = SessionStore(config.db_path)
    await store.init()
    return AppState(
        config=config,
        store=store,
        runner=runner if runner is not None else SpyRunner(),
        scheduler=Scheduler(config.max_concurrency),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """The trusted active workspace root chosen for a job (outside permanent roots)."""
    d = tmp_path / "workspace"
    (d / "pkg").mkdir(parents=True)
    return d


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """An existing directory that is not inside the job's project_root."""
    d = tmp_path / "elsewhere"
    d.mkdir()
    return d


@pytest.fixture
async def scoped_app(tmp_db_path: Path, workspace: Path, tmp_path: Path) -> AppState:
    """App with an EMPTY permanent trust list: only project_root can authorize cwd."""
    permanent = tmp_path / "permanent"
    permanent.mkdir()
    app = await _make_app(_config(tmp_db_path, allowed_roots=[permanent]))
    yield app
    await app.close()


# --- config / startup --------------------------------------------------------

def test_empty_allowed_project_roots_is_valid_and_grants_nothing(monkeypatch, tmp_path):
    """ALLOWED_PROJECT_ROOTS is optional; empty must not become a drive-wide grant."""
    monkeypatch.delenv("ALLOWED_PROJECT_ROOTS", raising=False)
    monkeypatch.setenv("CODEX_CLAUDE_AGENT_MCP_DB_PATH", str(tmp_path / "state.db"))
    cfg = Config.from_env()
    assert cfg.allowed_roots == []
    assert cfg.allow_any_cwd is False
    # The drive root is still rejected: no permanent grant was invented.
    with pytest.raises(MCPError) as exc:
        cfg.authorize_cwd(str(Path(tmp_path.anchor)))
    assert exc.value.code == INVALID_CWD


def test_configured_allowed_project_roots_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_CLAUDE_AGENT_MCP_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("ALLOWED_PROJECT_ROOTS", str(tmp_path / "missing-root"))
    with pytest.raises(ValueError, match="missing directories"):
        Config.from_env()


def test_resolve_project_root_rejects_relative_and_missing(tmp_path):
    cfg = _config(tmp_path / "db.sqlite")
    assert cfg.resolve_project_root(None) is None
    with pytest.raises(MCPError) as rel:
        cfg.resolve_project_root(".")
    assert rel.value.code == INVALID_CWD
    assert rel.value.details["field"] == "project_root"
    with pytest.raises(MCPError) as missing:
        cfg.resolve_project_root(str(tmp_path / "nope"))
    assert missing.value.code == INVALID_CWD
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(MCPError):
        cfg.resolve_project_root(str(file_path))


# --- execute_task: establishing a job-scoped root ----------------------------

async def test_project_root_outside_allowed_roots_is_accepted(scoped_app, workspace):
    res = await _execute_task_core(
        scoped_app, "pr-1", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    assert res.error is None
    assert res.status == "COMPLETED"
    job = await scoped_app.store.get_job("pr-1")
    assert job["project_root"] == str(workspace.resolve())
    assert job["cwd"] == str(workspace.resolve())
    assert ("execute", str(workspace.resolve())) in scoped_app.runner.cwds  # type: ignore[attr-defined]


async def test_cwd_may_be_a_child_of_project_root(scoped_app, workspace):
    child = workspace / "pkg"
    res = await _execute_task_core(
        scoped_app, "pr-child", "do work", str(child), ["c"], None, None, 60, str(workspace),
    )
    assert res.status == "COMPLETED"
    job = await scoped_app.store.get_job("pr-child")
    assert job["cwd"] == str(child.resolve())
    assert job["project_root"] == str(workspace.resolve())


async def test_cwd_outside_project_root_is_rejected(scoped_app, workspace, outside):
    res = await _execute_task_core(
        scoped_app, "pr-2", "do work", str(outside), ["c"], None, None, 60, str(workspace),
    )
    assert res.error is not None
    assert res.error.code == INVALID_CWD
    assert res.error.details["project_root"] == str(workspace.resolve())
    assert await scoped_app.store.get_job("pr-2") is None
    assert scoped_app.runner.calls == []  # type: ignore[attr-defined]


async def test_cwd_cannot_escape_project_root_via_traversal(scoped_app, workspace, outside):
    escape = str(workspace / ".." / outside.name)  # resolves to an existing dir outside the root
    res = await _execute_task_core(
        scoped_app, "pr-3", "do work", escape, ["c"], None, None, 60, str(workspace),
    )
    assert res.error is not None
    assert res.error.code == INVALID_CWD
    assert await scoped_app.store.get_job("pr-3") is None


async def test_omitted_project_root_keeps_permanent_root_behavior(tmp_db_path, workspace, outside):
    app = await _make_app(_config(tmp_db_path, allowed_roots=[workspace]))
    try:
        allowed = await _execute_task_core(app, "perm-1", "t", str(workspace), ["c"], None, None, 60)
        assert allowed.status == "COMPLETED"
        job = await app.store.get_job("perm-1")
        assert job["project_root"] is None  # legacy basis, nothing invented

        rejected = await _execute_task_core(app, "perm-2", "t", str(outside), ["c"], None, None, 60)
        assert rejected.error is not None
        assert rejected.error.code == INVALID_CWD
    finally:
        await app.close()


async def test_project_root_is_required_to_exist_at_call_time(scoped_app, outside):
    res = await _execute_task_core(
        scoped_app, "pr-4", "t", str(outside), ["c"], None, None, 60, str(outside / "missing"),
    )
    assert res.error is not None
    assert res.error.code == INVALID_CWD
    assert res.error.details["field"] == "project_root"


# --- continue_task: inherit, never enlarge -----------------------------------

async def test_continue_inherits_stored_temporary_root(scoped_app, workspace):
    er = await _execute_task_core(
        scoped_app, "pr-c1", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    cr = await _continue_task_core(
        scoped_app, "pr-c1", er.execution_session_id, "feedback", str(workspace), None, None, 60,
    )
    assert cr.error is None
    assert cr.status == "COMPLETED"
    assert ("continue", str(workspace.resolve())) in scoped_app.runner.cwds  # type: ignore[attr-defined]


async def test_continue_cannot_escape_stored_root(scoped_app, workspace, outside):
    er = await _execute_task_core(
        scoped_app, "pr-c2", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    res = await _continue_task_core(
        scoped_app, "pr-c2", er.execution_session_id, "feedback", str(outside), None, None, 60,
    )
    assert res.error is not None
    assert res.error.code == INVALID_CWD  # outside the persisted job root
    assert not [c for c in scoped_app.runner.calls if c[0] == "continue"]  # type: ignore[attr-defined]


async def test_continue_cannot_switch_cwd_inside_stored_root(scoped_app, workspace):
    er = await _execute_task_core(
        scoped_app, "pr-c3", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    res = await _continue_task_core(
        scoped_app, "pr-c3", er.execution_session_id, "feedback",
        str(workspace / "pkg"), None, None, 60,
    )
    assert res.error is not None
    assert res.error.code == JOB_CONTEXT_MISMATCH
    assert res.error.details["mismatched_fields"] == ["cwd"]


async def test_scoped_job_still_binds_the_persisted_session(scoped_app, workspace):
    first = await _execute_task_core(
        scoped_app, "pr-c4", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    other = await _execute_task_core(
        scoped_app, "pr-c5", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    res = await _continue_task_core(
        scoped_app, "pr-c4", other.execution_session_id, "feedback", str(workspace), None, None, 60,
    )
    assert res.error is not None
    assert res.error.code == "SESSION_MISMATCH"
    stored = await scoped_app.store.get_job("pr-c4")
    assert stored["execution_session_id"] == first.execution_session_id


async def test_continue_of_permanent_root_job_still_uses_allowed_roots(tmp_db_path, workspace, outside):
    app = await _make_app(_config(tmp_db_path, allowed_roots=[workspace]))
    try:
        er = await _execute_task_core(app, "perm-c1", "t", str(workspace), ["c"], None, None, 60)
        ok = await _continue_task_core(
            app, "perm-c1", er.execution_session_id, "feedback", str(workspace), None, None, 60,
        )
        assert ok.status == "COMPLETED"
        escaped = await _continue_task_core(
            app, "perm-c1", er.execution_session_id, "feedback", str(outside), None, None, 60,
        )
        assert escaped.error is not None
        assert escaped.error.code == INVALID_CWD
    finally:
        await app.close()


def test_continue_has_no_project_root_input():
    """A resume must have no way to name (and therefore enlarge) a root."""
    assert "project_root" not in ContinueTaskInput.model_fields


# --- review_task: inherit or match -------------------------------------------

async def test_review_inherits_stored_root_without_project_root(scoped_app, workspace):
    er = await _execute_task_core(
        scoped_app, "pr-r1", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    rv = await _review_task_core(
        scoped_app, "pr-r1", "do work", ["c"], str(workspace), er.summary, None, 60,
    )
    assert rv.error is None
    assert rv.status == "PASS"


async def test_review_accepts_matching_project_root(scoped_app, workspace):
    er = await _execute_task_core(
        scoped_app, "pr-r2", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    rv = await _review_task_core(
        scoped_app, "pr-r2", "do work", ["c"], str(workspace), er.summary, None, 60, str(workspace),
    )
    assert rv.status == "PASS"


async def test_review_rejects_parent_root_expansion(scoped_app, workspace):
    """A wider (parent) root must not enlarge an existing job's authorization."""
    er = await _execute_task_core(
        scoped_app, "pr-r7", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    rv = await _review_task_core(
        scoped_app, "pr-r7", "do work", ["c"], str(workspace), er.summary, None, 60,
        str(workspace.parent),
    )
    assert rv.error is not None
    assert rv.error.code == JOB_CONTEXT_MISMATCH
    assert rv.error.details["mismatched_fields"] == ["project_root"]


async def test_reexecute_cannot_switch_project_root(scoped_app, workspace, outside):
    first = await _execute_task_core(
        scoped_app, "pr-re", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    assert first.status == "COMPLETED"
    again = await _execute_task_core(
        scoped_app, "pr-re", "do work", str(outside), ["c"], None, None, 60, str(outside),
    )
    assert again.error is not None
    assert again.error.code == "STATE_STORE_ERROR"
    job = await scoped_app.store.get_job("pr-re")
    assert job["project_root"] == str(workspace.resolve())
    assert job["cwd"] == str(workspace.resolve())


async def test_review_rejects_switching_project_root(scoped_app, workspace, outside):
    er = await _execute_task_core(
        scoped_app, "pr-r3", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    before = len(scoped_app.runner.calls)  # type: ignore[attr-defined]
    rv = await _review_task_core(
        scoped_app, "pr-r3", "do work", ["c"], str(outside), er.summary, None, 60, str(outside),
    )
    assert rv.error is not None
    assert rv.error.code == JOB_CONTEXT_MISMATCH
    assert rv.error.details["mismatched_fields"] == ["project_root"]
    assert len(scoped_app.runner.calls) == before  # type: ignore[attr-defined]
    job = await scoped_app.store.get_job("pr-r3")
    assert job["project_root"] == str(workspace.resolve())  # unchanged


async def test_review_cwd_must_stay_inside_stored_root(scoped_app, workspace):
    """Omitting project_root is allowed (inherit), but cwd must stay inside it."""
    er = await _execute_task_core(
        scoped_app, "pr-r4", "do work", str(workspace), ["c"], None, None, 60, str(workspace),
    )
    rv = await _review_task_core(
        scoped_app, "pr-r4", "do work", ["c"], str(workspace.parent), er.summary, None, 60,
    )
    assert rv.error is not None
    assert rv.error.code == INVALID_CWD


async def test_review_cannot_add_project_root_to_permanent_root_job(tmp_db_path, workspace, outside):
    app = await _make_app(_config(tmp_db_path, allowed_roots=[workspace]))
    try:
        er = await _execute_task_core(app, "perm-r1", "t", str(workspace), ["c"], None, None, 60)
        rv = await _review_task_core(
            app, "perm-r1", "t", ["c"], str(workspace), er.summary, None, 60, str(workspace),
        )
        assert rv.error is not None
        assert rv.error.code == JOB_CONTEXT_MISMATCH
        assert rv.error.details["mismatched_fields"] == ["project_root"]
    finally:
        await app.close()


async def test_review_only_new_job_may_establish_project_root(scoped_app, workspace):
    rv = await _review_task_core(
        scoped_app, "pr-r5", "task", ["c"], str(workspace), None, None, 60, str(workspace),
    )
    assert rv.error is None
    assert rv.status == "PASS"
    job = await scoped_app.store.get_job("pr-r5")
    assert job["project_root"] == str(workspace.resolve())
    assert job["status"] == "COMPLETED"  # review PASS -> COMPLETED


async def test_review_of_temporary_root_job_stays_read_only(tmp_db_path, workspace, monkeypatch):
    """The reviewer keeps Read/Grep/Glob only, even for a temporary root job."""
    runner = RealClaudeRunner()
    captured: dict[str, Any] = {}

    async def fake_run_query(prompt, options, timeout_sec, on_session_id=None, **kwargs):
        captured["options"] = options
        sid = options.session_id
        if on_session_id is not None:
            await on_session_id(sid)
        return SimpleNamespace(
            session_id=sid, total_cost_usd=None, is_error=False,
            structured_output={"verdict": "PASS", "summary": "ok", "unmet_criteria": [], "evidence": []},
            result=None,
        ), "", sid, None

    monkeypatch.setattr(runner, "_run_query", fake_run_query)
    app = await _make_app(_config(tmp_db_path), runner)  # empty permanent roots
    try:
        rv = await _review_task_core(
            app, "pr-r6", "task", ["c"], str(workspace), None, None, 60, str(workspace),
        )
        assert rv.status == "PASS"
        options = captured["options"]
        assert Path(options.cwd) == workspace.resolve()
        assert options.allowed_tools == ["Read", "Grep", "Glob"]
        assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} <= set(options.disallowed_tools)
    finally:
        await app.close()


# --- run_job / run_jobs propagation ------------------------------------------

async def test_run_job_propagates_project_root(scoped_app, workspace):
    res = await _run_job_core(
        scoped_app,
        JobSpec(job_id="pr-j1", task="do work", cwd=str(workspace), project_root=str(workspace),
                acceptance=["c"], review=True),
    )
    assert res.execution.error is None
    assert res.execution.status == "COMPLETED"
    assert res.review.status == "PASS"
    assert res.status == "COMPLETED"
    job = await scoped_app.store.get_job("pr-j1")
    assert job["project_root"] == str(workspace.resolve())


async def test_run_job_without_project_root_keeps_legacy_check(scoped_app, outside):
    rejected = await _run_job_core(
        scoped_app, JobSpec(job_id="pr-j2", task="t", cwd=str(outside), review=False),
    )
    assert rejected.execution.error is not None
    assert rejected.execution.error.code == INVALID_CWD
    assert await scoped_app.store.get_job("pr-j2") is None


async def test_run_jobs_propagates_per_job_project_root(scoped_app, workspace, outside, tmp_path):
    other = tmp_path / "second-workspace"
    other.mkdir()
    jobs = [
        JobSpec(job_id="pr-b1", task="t1", cwd=str(workspace), project_root=str(workspace),
                acceptance=["c"], review=True),
        JobSpec(job_id="pr-b2", task="t2", cwd=str(other), project_root=str(other),
                acceptance=["c"], review=True),
        JobSpec(job_id="pr-b3", task="t3", cwd=".", acceptance=["c"], review=True),  # no root -> rejected
    ]
    res = await _run_jobs_core(scoped_app, jobs, None)
    assert res.error is None
    by_id = {r.job_id: r for r in res.results}
    assert by_id["pr-b1"].status == "COMPLETED"
    assert by_id["pr-b2"].status == "COMPLETED"
    assert by_id["pr-b3"].execution.error.code == INVALID_CWD
    assert (await scoped_app.store.get_job("pr-b1"))["project_root"] == str(workspace.resolve())
    assert (await scoped_app.store.get_job("pr-b2"))["project_root"] == str(other.resolve())


async def test_run_jobs_rejects_root_that_does_not_contain_cwd(scoped_app, workspace, outside):
    res = await _run_jobs_core(
        scoped_app,
        [JobSpec(job_id="pr-b4", task="t", cwd=str(outside), project_root=str(workspace), review=False)],
        None,
    )
    assert res.results[0].execution.error.code == INVALID_CWD


# --- MCP input schema --------------------------------------------------------

async def test_tool_schemas_expose_project_root_except_continue():
    import warnings

    from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning
    from codex_claude_agent_mcp.server import build_server

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", IncompleteFieldDefinitionWarning)
        tools = {t.name: t for t in await build_server().list_tools()}

    def _props(schema: dict) -> dict:
        return schema.get("properties", {})

    for name in ("execute_task", "review_task", "run_job"):
        schema = tools[name].inputSchema
        assert "project_root" in _props(schema), name
    # The description travels with the schema so Codex sees the contract.
    assert "never inferred from the MCP process cwd" in str(
        _props(tools["execute_task"].inputSchema)["project_root"]
    )
    jobs = _props(tools["run_jobs"].inputSchema)["jobs"]
    spec = tools["run_jobs"].inputSchema["$defs"]["JobSpec"]
    assert jobs["items"]["$ref"].endswith("/JobSpec")
    assert "project_root" in spec["properties"]
    # continue_task must not expose any way to name a root.
    assert "project_root" not in _props(tools["continue_task"].inputSchema)
