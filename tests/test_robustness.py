"""Robustness pass tests.

Covers: buffer-size configuration/passthrough, provider billing (402)
classification, non-retryable protocol errors, earliest-session capture and
persistence on timeout/transport failure, incomplete-review result shape,
run_job REVIEW_INCOMPLETE stage statuses, get_job_status, and ping runtime
metadata. No real Claude provider call is made anywhere.
"""

from __future__ import annotations

import asyncio
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk
import pytest

from codex_claude_agent_mcp.claude_runner import (
    ExecutionResult,
    FakeClaudeRunner,
    RealClaudeRunner,
    ClaudeRunner,
    resolve_cli_path,
)
from codex_claude_agent_mcp.claude_runner import ReviewResult
from codex_claude_agent_mcp.config import Config, reset_config
from codex_claude_agent_mcp.errors import (
    CLAUDE_BILLING_ERROR,
    CLAUDE_PROVIDER_ERROR,
    CLAUDE_PROTOCOL_ERROR,
    CLAUDE_SESSION_ERROR,
    INVALID_ARGUMENT,
    MCPError,
    RATE_LIMITED,
    SESSION_NOT_FOUND,
    TASK_TIMEOUT,
)
from codex_claude_agent_mcp.models import JobSpec
from codex_claude_agent_mcp.server import (
    AppState,
    _execute_task_core,
    _continue_task_core,
    _get_job_status_core,
    _ping_metadata,
    _review_task_core,
    _run_job_core,
)

DEFAULT_20_MIB = 20 * 1024 * 1024


def _env_config(monkeypatch, tmp_path, **env) -> None:
    monkeypatch.setenv("CODEX_CLAUDE_AGENT_MCP_DB_PATH", str(tmp_path / "db"))
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


# --- Buffer size configuration -----------------------------------------------


def test_config_default_buffer_is_20mib(monkeypatch, tmp_path):
    _env_config(monkeypatch, tmp_path, CLAUDE_SDK_MAX_BUFFER_SIZE=None)
    reset_config()
    try:
        assert Config.from_env().max_buffer_size == DEFAULT_20_MIB
    finally:
        reset_config()


def test_config_env_buffer_override(monkeypatch, tmp_path):
    _env_config(monkeypatch, tmp_path, CLAUDE_SDK_MAX_BUFFER_SIZE="1048576")
    reset_config()
    try:
        assert Config.from_env().max_buffer_size == 1048576
    finally:
        reset_config()


def test_config_rejects_absurdly_small_buffer(monkeypatch, tmp_path):
    _env_config(monkeypatch, tmp_path, CLAUDE_SDK_MAX_BUFFER_SIZE="512")
    reset_config()
    try:
        with pytest.raises(ValueError):
            Config.from_env()
    finally:
        reset_config()


def test_buffer_size_passed_to_all_claude_agent_options(tmp_path):
    runner = RealClaudeRunner(cli_path="C:/x/claude.exe", max_buffer_size=1234567)
    execution = runner._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j", resume=None,
    )
    review = runner._review_options(cwd=str(tmp_path), model=None, job_id="j")
    assert execution.max_buffer_size == review.max_buffer_size == 1234567
    assert execution.cli_path == review.cli_path == "C:/x/claude.exe"

    default = RealClaudeRunner()
    assert default._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j", resume=None,
    ).max_buffer_size == DEFAULT_20_MIB
    assert default._review_options(
        cwd=str(tmp_path), model=None, job_id="j",
    ).max_buffer_size == DEFAULT_20_MIB


def test_fresh_sessions_are_preallocated_but_resume_only_uses_resume(tmp_path):
    runner = RealClaudeRunner()
    sid = str(uuid.uuid4())
    execution = runner._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j",
        resume=None, session_id=sid,
    )
    review = runner._review_options(
        cwd=str(tmp_path), model=None, job_id="j", session_id=sid,
    )
    resumed = runner._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j",
        resume=sid, session_id=None,
    )
    assert execution.session_id == review.session_id == sid
    assert execution.resume is None and review.resume is None
    assert resumed.resume == sid and resumed.session_id is None


async def test_appstate_runner_uses_config_buffer(config, monkeypatch):
    config.max_buffer_size = 424242
    config.cli_path = "C:/x/claude.exe"
    app = await AppState.create(config=config)
    try:
        assert isinstance(app.runner, RealClaudeRunner)
        assert app.runner.max_buffer_size == 424242
        assert app.runner.cli_path == "C:/x/claude.exe"
    finally:
        await app.close()


# --- Billing (402 / insufficient balance) classification ----------------------


def _error_result_msg(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="sid-402",
        total_cost_usd=None, result=None, structured_output=None,
        errors=[], api_error_status=402,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_402_with_success_subtype_and_empty_errors_is_billing(monkeypatch, tmp_path):
    """The real CLI shape: is_error + api_error_status=402, subtype success, errors=[].

    The concise real provider text (``result``) must be preserved in the message.
    """
    runner = RealClaudeRunner()
    result_msg = _error_result_msg(
        result="Your credit balance is too low to access the Anthropic API.",
    )

    async def fake_run_query(prompt, options, timeout_sec, on_session_id=None, **kwargs):
        return result_msg, "", "sid-402", None

    monkeypatch.setattr(runner, "_run_query", fake_run_query)
    res = await runner.execute(
        task="t", cwd=str(tmp_path), acceptance=[], model=None,
        effort=None, timeout_sec=5, job_id="j",
    )
    assert res.status == "FAILED"
    assert res.error is not None
    assert res.error.code == CLAUDE_BILLING_ERROR
    assert res.error.retryable is False
    assert "credit balance is too low" in res.error.message
    assert res.error.details["status"] == 402
    # The known session is still returned with the failure.
    assert res.session_id == "sid-402"


async def test_real_sdk_402_stream_shape_and_trailing_exception(monkeypatch, tmp_path):
    """Matches the observed SDK/CLI failure without contacting the provider."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

    sid = str(uuid.uuid4())

    async def stream(*, prompt, options):
        assert options.session_id == sid
        yield SystemMessage(subtype="init", data={"session_id": sid})
        yield AssistantMessage(
            content=[TextBlock("API Error: 402 Insufficient Balance")],
            model="test-model", error="billing_error", session_id=sid,
        )
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=1, session_id=sid,
            errors=[], api_error_status=None,
        )
        raise RuntimeError("Error result: success")

    monkeypatch.setattr(claude_agent_sdk, "query", stream)
    res = await RealClaudeRunner().execute(
        task="t", cwd=str(tmp_path), acceptance=[], model=None, effort=None,
        timeout_sec=5, session_id=sid, job_id="billing-stream",
    )
    assert res.error is not None
    assert res.error.code == CLAUDE_BILLING_ERROR
    assert res.error.retryable is False
    assert "API Error: 402 Insufficient Balance" in res.error.message
    assert res.session_id == sid and res.session_confirmed is True
    assert res.error.details["stage"] == "execution"
    assert res.error.details["session_id"] == sid


def test_classify_billing_from_insufficient_balance_text():
    msg = SimpleNamespace(
        api_error_status=None, errors=["Error: insufficient balance for model"], result=None,
    )
    err = RealClaudeRunner._classify_error(msg)
    assert err is not None
    assert err.code == CLAUDE_BILLING_ERROR
    assert err.retryable is False
    assert "insufficient balance" in err.message


def test_billing_is_not_masked_as_auth_or_rate_limit():
    msg = SimpleNamespace(api_error_status=402, errors=[], result="payment required")
    err = RealClaudeRunner._classify_error(msg)
    assert err is not None and err.code == CLAUDE_BILLING_ERROR


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(408, CLAUDE_PROVIDER_ERROR, True), (429, RATE_LIMITED, True),
     (500, CLAUDE_PROVIDER_ERROR, True), (529, CLAUDE_PROVIDER_ERROR, True),
     (404, CLAUDE_PROVIDER_ERROR, False)],
)
def test_provider_status_retry_policy(status, code, retryable):
    err = RealClaudeRunner._classify_error(
        SimpleNamespace(api_error_status=status, errors=[], result=f"HTTP {status}")
    )
    assert err is not None and err.code == code
    assert err.retryable is retryable


@pytest.mark.parametrize(
    ("diagnostic", "code", "retryable"),
    [("rate_limit", RATE_LIMITED, True),
     ("server_error", CLAUDE_PROVIDER_ERROR, True),
     ("invalid_request", CLAUDE_PROVIDER_ERROR, False)],
)
def test_sdk_error_kind_retry_policy(diagnostic, code, retryable):
    err = RealClaudeRunner._classify_error(None, diagnostic=diagnostic)
    assert err is not None and err.code == code
    assert err.retryable is retryable


# --- Protocol errors (invalid / contradictory structured results) -------------


async def _run_with_result_msg(monkeypatch, tmp_path, result_msg, method="review"):
    runner = RealClaudeRunner()

    async def fake_run_query(prompt, options, timeout_sec, on_session_id=None, **kwargs):
        return result_msg, "", getattr(result_msg, "session_id", None), None

    monkeypatch.setattr(runner, "_run_query", fake_run_query)
    if method == "review":
        return await runner.review(
            original_task="t", acceptance=["c"], cwd=str(tmp_path),
            execution_summary=None, model=None, timeout_sec=5, job_id="j",
        )
    return await runner.execute(
        task="t", cwd=str(tmp_path), acceptance=[], model=None,
        effort=None, timeout_sec=5, job_id="j",
    )


async def test_unparseable_review_output_is_non_retryable_protocol_error(monkeypatch, tmp_path):
    result_msg = SimpleNamespace(
        session_id="sid-1", total_cost_usd=None, is_error=False,
        structured_output=None, result="prose without any json object",
    )
    res = await _run_with_result_msg(monkeypatch, tmp_path, result_msg)
    assert res.status == "FAILED"
    assert res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.error.retryable is False
    assert res.session_id == "sid-1"


async def test_pass_with_unmet_criteria_is_contradictory_protocol_error(monkeypatch, tmp_path):
    result_msg = SimpleNamespace(
        session_id="sid-2", total_cost_usd=None, is_error=False,
        structured_output={
            "verdict": "PASS", "summary": "s", "unmet_criteria": ["criterion A"], "evidence": [],
        },
        result=None,
    )
    res = await _run_with_result_msg(monkeypatch, tmp_path, result_msg)
    assert res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.error.retryable is False


async def test_fail_without_unmet_criteria_is_contradictory_protocol_error(monkeypatch, tmp_path):
    result_msg = SimpleNamespace(
        session_id="sid-3", total_cost_usd=None, is_error=False,
        structured_output={
            "verdict": "FAIL", "summary": "s", "unmet_criteria": [], "evidence": ["e.py:1"],
        },
        result=None,
    )
    res = await _run_with_result_msg(monkeypatch, tmp_path, result_msg)
    assert res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.error.retryable is False


async def test_fail_without_evidence_is_contradictory_protocol_error(monkeypatch, tmp_path):
    result_msg = SimpleNamespace(
        session_id="sid-3b", total_cost_usd=None, is_error=False,
        structured_output={
            "verdict": "FAIL", "summary": "s",
            "unmet_criteria": ["criterion A"], "evidence": [],
        },
        result=None,
    )
    res = await _run_with_result_msg(monkeypatch, tmp_path, result_msg)
    assert res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.completed is False


async def test_unparseable_execution_output_is_non_retryable_protocol_error(monkeypatch, tmp_path):
    result_msg = SimpleNamespace(
        session_id="sid-4", total_cost_usd=None, is_error=False,
        structured_output=None, result="no json object here",
    )
    res = await _run_with_result_msg(monkeypatch, tmp_path, result_msg, method="execute")
    assert res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.error.retryable is False


@pytest.mark.parametrize("method", ["execute", "review"])
async def test_missing_terminal_result_is_incomplete_protocol_error(monkeypatch, tmp_path, method):
    runner = RealClaudeRunner()

    async def fake_run_query(*args, **kwargs):
        # Even plausible JSON text cannot prove completion without ResultMessage.
        text = ('{"status":"COMPLETED","summary":"s","files_changed":[],"validation":[]}'
                if method == "execute" else
                '{"verdict":"PASS","summary":"s","unmet_criteria":[],"evidence":[]}')
        return None, text, "sid-no-terminal", None

    monkeypatch.setattr(runner, "_run_query", fake_run_query)
    if method == "execute":
        res = await runner.execute(
            task="t", cwd=str(tmp_path), acceptance=[], model=None,
            effort=None, timeout_sec=5, job_id="missing-terminal",
        )
    else:
        res = await runner.review(
            original_task="t", acceptance=["c"], cwd=str(tmp_path),
            execution_summary=None, model=None, timeout_sec=5,
            job_id="missing-terminal",
        )
    assert res.completed is False
    assert res.error is not None and res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.error.retryable is False


# --- Earliest session capture & persistence on timeout/transport failure ------


async def test_earliest_streamed_session_survives_timeout(monkeypatch):
    """A session id seen in an early stream message is returned on timeout."""

    async def _stream(*, prompt, options):
        yield SimpleNamespace(subtype="init", data={"session_id": "sid-stream"})
        await asyncio.sleep(30)
        yield None  # pragma: no cover - never reached

    monkeypatch.setattr(
        claude_agent_sdk, "query", lambda *, prompt, options: _stream(prompt=prompt, options=options),
    )
    runner = RealClaudeRunner()
    seen: list[str] = []

    async def on_session_id(session_id: str) -> None:
        seen.append(session_id)

    res = await runner.execute(
        task="t", cwd=".", acceptance=[], model=None, effort=None,
        timeout_sec=1, job_id="j", on_session_id=on_session_id,
    )
    assert res.status == "FAILED"
    assert res.error is not None and res.error.code == TASK_TIMEOUT
    assert res.session_id == "sid-stream"
    assert res.error.details.get("session_id") == "sid-stream"
    assert seen == ["sid-stream"]


class _EarlySessionTimeoutRunner(ClaudeRunner):
    """Fires the narrow session callback, then times out like a real run."""

    async def execute(self, *, session_id=None, on_session_id=None, **kwargs: Any):
        if on_session_id is not None:
            await on_session_id(session_id)
        raise MCPError(TASK_TIMEOUT, "timed out", retryable=True, details={
            "session_id": session_id, "session_confirmed": True,
        })

    async def review(self, **kwargs: Any):
        raise MCPError(CLAUDE_SESSION_ERROR, "no review", retryable=True)

    async def continue_session(self, **kwargs: Any):
        raise MCPError(TASK_TIMEOUT, "no continue", retryable=True)


class _TransportFailRunner(ClaudeRunner):
    """Fires the narrow session callback, then fails at the transport level."""

    async def execute(self, *, session_id=None, on_session_id=None, **kwargs: Any):
        if on_session_id is not None:
            await on_session_id(session_id)
        raise RuntimeError("pipe broke")

    async def review(self, **kwargs: Any):
        raise RuntimeError("pipe broke")

    async def continue_session(self, **kwargs: Any):
        raise RuntimeError("pipe broke")


async def test_timeout_returns_and_persists_earliest_session(app):
    app.runner = _EarlySessionTimeoutRunner()
    res = await _execute_task_core(app, "early-1", "t", ".", [], None, None, 60)
    assert res.status == "FAILED"
    assert res.execution_session_id
    assert res.execution_session_confirmed is True
    job = await app.store.get_job("early-1")
    assert job["execution_session_id"] == res.execution_session_id
    assert job["execution_session_confirmed"] is True
    assert job["status"] == "EXECUTION_INCOMPLETE"
    # The persisted session remains resumable.
    stored = await app.store.get_job("early-1")
    assert stored["execution_session_id"] == res.execution_session_id


async def test_transport_failure_retains_session_via_callback_binding(app):
    app.runner = _TransportFailRunner()
    res = await _execute_task_core(app, "early-2", "t", ".", [], None, None, 60)
    assert res.status == "FAILED"
    job = await app.store.get_job("early-2")
    assert job["execution_session_id"] == res.execution_session_id
    assert job["execution_session_confirmed"] is True


async def test_unconfirmed_preallocated_session_is_retained_but_not_resumable(app):
    class _FailsBeforeInit(_TransportFailRunner):
        async def execute(self, **kwargs: Any):
            raise RuntimeError("failed before init")

    app.runner = _FailsBeforeInit()
    res = await _execute_task_core(app, "unconfirmed-1", "t", ".", [], None, None, 60)
    assert res.execution_session_id
    assert res.execution_session_confirmed is False
    job = await app.store.get_job("unconfirmed-1")
    assert job["execution_session_id"] == res.execution_session_id
    assert job["execution_session_confirmed"] is False
    assert job["execution_error"]["details"]["stage"] == "execution"

    app.runner = FakeClaudeRunner()
    continued = await _continue_task_core(
        app, "unconfirmed-1", res.execution_session_id, "retry", ".", None, None, 60,
    )
    assert continued.error is not None
    assert continued.error.code == SESSION_NOT_FOUND


async def test_confirmed_timeout_session_can_continue(app):
    app.runner = _EarlySessionTimeoutRunner()
    first = await _execute_task_core(app, "confirmed-1", "t", ".", [], None, None, 60)
    assert first.execution_session_confirmed is True

    app.runner = FakeClaudeRunner()
    resumed = await _continue_task_core(
        app, "confirmed-1", first.execution_session_id, "finish", ".", None, None, 60,
    )
    assert resumed.status == "COMPLETED"
    assert resumed.execution_completed is True
    assert resumed.execution_session_id == first.execution_session_id


async def test_runner_cannot_switch_preallocated_session(app):
    class _WrongSessionRunner(ClaudeRunner):
        async def execute(self, **kwargs: Any):
            return ExecutionResult(
                status="COMPLETED", completed=True, session_id=str(uuid.uuid4()),
                session_confirmed=True, summary="wrong",
            )

        async def review(self, **kwargs: Any):
            raise AssertionError("unused")

        async def continue_session(self, **kwargs: Any):
            raise AssertionError("unused")

    app.runner = _WrongSessionRunner()
    res = await _execute_task_core(app, "mismatch-1", "t", ".", [], None, None, 60)
    assert res.error is not None and res.error.code == CLAUDE_PROTOCOL_ERROR
    assert res.execution_completed is False
    job = await app.store.get_job("mismatch-1")
    assert job["status"] == "EXECUTION_INCOMPLETE"
    assert job["execution_session_confirmed"] is False


async def test_review_error_persists_review_session_and_marks_incomplete(app):
    class _ReviewSessionErrorRunner(ClaudeRunner):
        async def execute(self, **kwargs: Any):
            return await FakeClaudeRunner().execute(**kwargs)

        async def review(self, *, session_id=None, on_session_id=None, **kwargs: Any):
            if on_session_id is not None:
                await on_session_id(session_id)
            raise MCPError(CLAUDE_SESSION_ERROR, "transport died", retryable=True,
                           details={"session_id": session_id, "session_confirmed": True})

        async def continue_session(self, **kwargs: Any):
            raise MCPError(CLAUDE_SESSION_ERROR, "no continue", retryable=True)

    app.runner = _ReviewSessionErrorRunner()
    res = await _review_task_core(app, "early-3", "task", ["c"], ".", None, None, 60)
    assert res.status == "FAILED"
    assert res.review_session_id
    assert res.review_session_confirmed is True
    job = await app.store.get_job("early-3")
    assert job["review_session_id"] == res.review_session_id
    assert job["status"] == "REVIEW_INCOMPLETE"
    assert job["review_status"] == "INCOMPLETE"


# --- Incomplete review shape --------------------------------------------------


async def test_incomplete_review_has_null_arrays_but_completed_keeps_them(app):
    # Incomplete (error) review: review_completed=false, arrays null.
    class _FailingReviewRunner(ClaudeRunner):
        async def execute(self, **kwargs: Any):
            return await FakeClaudeRunner().execute(**kwargs)

        async def review(self, **kwargs: Any):
            return ReviewResult(
                status="FAILED", session_id="rs-9",
                error=MCPError(CLAUDE_SESSION_ERROR, "provider down", retryable=True),
            )

        async def continue_session(self, **kwargs: Any):
            raise MCPError(CLAUDE_SESSION_ERROR, "no continue", retryable=True)

    app.runner = _FailingReviewRunner()
    res = await _review_task_core(app, "shape-1", "task", ["c"], ".", None, None, 60)
    assert res.status == "FAILED"
    assert res.review_completed is False
    assert res.unmet_criteria is None
    assert res.evidence is None
    assert res.error is not None

    # Completed PASS keeps the (empty) arrays.
    app.runner = FakeClaudeRunner()
    await _execute_task_core(app, "shape-2", "task", ".", ["c"], None, None, 60)
    ok = await _review_task_core(app, "shape-2", "task", ["c"], ".", "done", None, 60)
    assert ok.review_completed is True
    assert ok.unmet_criteria == []
    assert ok.evidence == ["all good"]

    # Completed FAIL keeps both arrays populated.
    app.runner.review_mode = "review_fail"
    fail = await _review_task_core(app, "shape-2", "task", ["c"], ".", "done", None, 60)
    assert fail.status == "FAIL"
    assert fail.review_completed is True
    assert fail.unmet_criteria == ["criterion A not met"]
    assert fail.evidence == ["evidence.py:10"]


# --- run_job REVIEW_INCOMPLETE stage statuses ---------------------------------


class _ExecOkReviewErrorRunner(ClaudeRunner):
    """Execution succeeds (delegates to the fake); review always errors."""

    def __init__(self) -> None:
        self._fake = FakeClaudeRunner()

    async def execute(self, **kwargs: Any):
        return await self._fake.execute(**kwargs)

    async def review(self, **kwargs: Any):
        return ReviewResult(
            status="FAILED", session_id=kwargs.get("session_id"),
            error=MCPError(CLAUDE_SESSION_ERROR, "provider down", retryable=True),
        )

    async def continue_session(self, **kwargs: Any):
        return await self._fake.continue_session(**kwargs)


async def test_run_job_review_incomplete_stage_status(app):
    app.runner = _ExecOkReviewErrorRunner()
    res = await _run_job_core(
        app, JobSpec(job_id="ri-1", task="t", cwd=".", acceptance=["c"], review=True),
    )
    assert res.status == "REVIEW_INCOMPLETE"
    assert res.execution_status == "COMPLETED"
    assert res.review_status == "INCOMPLETE"
    assert res.execution.status == "COMPLETED"
    assert res.review is not None
    assert res.review.review_completed is False
    assert res.review.unmet_criteria is None and res.review.evidence is None

    job = await app.store.get_job("ri-1")
    assert job["status"] == "REVIEW_INCOMPLETE"
    assert job["execution_status"] == "COMPLETED"
    assert job["review_status"] == "INCOMPLETE"

    # Recovery: REVIEW_INCOMPLETE is claimable for a fresh review.
    app.runner = FakeClaudeRunner()
    rv = await _review_task_core(app, "ri-1", "t", ["c"], ".", res.execution.summary, None, 60)
    assert rv.status == "PASS"
    job = await app.store.get_job("ri-1")
    assert job["status"] == "COMPLETED"
    assert job["review_status"] == "PASS"


async def test_run_job_without_review_keeps_stage_status(app):
    res = await _run_job_core(
        app, JobSpec(job_id="ri-2", task="t", cwd=".", acceptance=[], review=False),
    )
    assert res.status == "COMPLETED"
    assert res.execution_status == "COMPLETED"
    assert res.review_status is None
    assert res.review is None


# --- get_job_status -----------------------------------------------------------


async def test_get_job_status_found_missing_and_invalid(app):
    missing = await _get_job_status_core(app, "no-such-job", ".")
    assert missing.found is False
    assert missing.error is None

    await _execute_task_core(app, "gs-1", "task text", ".", ["c1", "c2"], None, None, 60)
    status = await _get_job_status_core(app, "gs-1", ".")
    assert status.found is True
    assert status.status == "COMPLETED"
    assert status.execution_status == "COMPLETED"
    assert status.execution_session_id is not None
    assert status.review_session_id is None
    assert status.execution_session_confirmed is True
    assert status.created_at is not None and status.updated_at is not None

    bad = await _get_job_status_core(app, "   ", ".")
    assert bad.found is False
    assert bad.error is not None
    assert bad.error.code == INVALID_ARGUMENT


async def test_get_job_status_requires_the_bound_project(app, tmp_path):
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    await _execute_task_core(
        app, "gs-auth", "task", str(root), [], None, None, 60, str(root),
    )
    denied = await _get_job_status_core(app, "gs-auth", str(other))
    assert denied.found is False
    assert denied.error is not None
    assert denied.error.code in {"INVALID_CWD", "JOB_CONTEXT_MISMATCH"}


def test_package_version_is_consistent():
    from codex_claude_agent_mcp import __version__

    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock_text = (root / "uv.lock").read_text(encoding="utf-8")
    assert project["project"]["version"] == __version__
    assert f'name = "codex-claude-agent-mcp"\nversion = "{__version__}"' in lock_text


# --- ping runtime metadata ----------------------------------------------------


def test_ping_runtime_metadata(monkeypatch, tmp_path):
    _env_config(monkeypatch, tmp_path, CLAUDE_SDK_MAX_BUFFER_SIZE="1048576")
    reset_config()
    try:
        meta = _ping_metadata()
        assert meta.ok is True
        assert meta.pid > 0
        assert meta.version  # server version
        assert meta.sdk_version == claude_agent_sdk.__version__
        assert meta.max_buffer_size == 1048576
        # The bundled CLI ships with the SDK in this environment.
        assert meta.cli_path is not None and Path(meta.cli_path).is_file()
        assert meta.cli_version  # bundled CLI version is known without spawning
    finally:
        reset_config()


def test_resolve_cli_path_prefers_explicit_and_falls_back():
    assert resolve_cli_path("C:/tools/claude.exe") == "C:/tools/claude.exe"
    resolved = resolve_cli_path(None)
    assert resolved is None or Path(resolved).is_file()
