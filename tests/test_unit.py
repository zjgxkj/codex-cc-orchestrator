"""Unit tests for review fixes: error codes, JSON extraction, stream cleanup, row mapping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from codex_claude_agent_mcp.claude_runner import RealClaudeRunner, _balanced_brace_spans, _extract_json_block
from codex_claude_agent_mcp.config import Config
from codex_claude_agent_mcp.errors import INVALID_ARGUMENT, MCPError, TASK_TIMEOUT


def _bare_config() -> Config:
    """A Config not tied to any DB (clamp_timeout never touches db_path)."""
    return Config(
        max_concurrency=1,
        default_task_timeout_sec=60,
        max_task_timeout_sec=120,
        db_path=None,  # type: ignore[arg-type]
        cli_path=None,
        allowed_roots=[],
        allowed_models=[],
        allow_any_cwd=True,
    )


# --- clamp_timeout error code -------------------------------------------------


def test_clamp_timeout_zero_is_invalid_argument() -> None:
    with pytest.raises(MCPError) as ei:
        _bare_config().clamp_timeout(0)
    assert ei.value.code == INVALID_ARGUMENT
    assert ei.value.details.get("field") == "timeout_sec"


def test_clamp_timeout_negative_is_invalid_argument() -> None:
    with pytest.raises(MCPError) as ei:
        _bare_config().clamp_timeout(-5)
    assert ei.value.code == INVALID_ARGUMENT


def test_clamp_timeout_pass_through() -> None:
    cfg = _bare_config()
    assert cfg.clamp_timeout(None) == 60
    assert cfg.clamp_timeout(30) == 30
    assert cfg.clamp_timeout(9999) == 120  # clamped to max


# --- _extract_json_block ------------------------------------------------------


def test_extract_bare_json_with_braces_in_summary() -> None:
    """Regression: braces inside string values must not break bare-text parsing."""
    text = '{"status": "COMPLETED", "summary": "see config {foo: 1}", "files_changed": ["a.py"]}'
    obj = _extract_json_block(text)
    assert obj is not None
    assert obj["status"] == "COMPLETED"
    assert obj["summary"] == "see config {foo: 1}"
    assert obj["files_changed"] == ["a.py"]


def test_extract_bare_json_with_unbalanced_brace_in_string() -> None:
    """A stray ``}`` inside a string value must not truncate the object."""
    text = 'prefix prose\n{"status": "BLOCKED", "summary": "close brace: } in text"}'
    obj = _extract_json_block(text)
    assert obj is not None
    assert obj["status"] == "BLOCKED"


def test_extract_bare_json_prefers_last_object() -> None:
    text = '{"other": 1}\n{"verdict": "PASS", "summary": "ok"}'
    obj = _extract_json_block(text)
    assert obj is not None
    assert obj["verdict"] == "PASS"


def test_extract_fenced_json_with_braces_still_works() -> None:
    text = 'prose\n```json\n{"status": "COMPLETED", "summary": "use {a}"}\n```'
    obj = _extract_json_block(text)
    assert obj is not None
    assert obj["status"] == "COMPLETED"


def test_extract_bare_json_without_status_or_verdict_is_none() -> None:
    assert _extract_json_block('{"hello": "world"}') is None
    assert _extract_json_block("") is None
    assert _extract_json_block("no json here") is None


def test_extract_nested_object_braces() -> None:
    text = '{"status": "COMPLETED", "detail": {"inner": "x}"}, "summary": "s"}'
    obj = _extract_json_block(text)
    assert obj is not None
    assert obj["detail"] == {"inner": "x}"}


def test_balanced_brace_spans_escapes() -> None:
    text = '{"status": "escaped quote \\" and brace } still inside"}'
    spans = _balanced_brace_spans(text)
    assert len(spans) == 1
    assert '"status"' in spans[0]


def test_balanced_brace_spans_unterminated() -> None:
    assert _balanced_brace_spans('{"status": "x"') == []
    assert _balanced_brace_spans("no braces") == []


def test_real_runner_uses_json_schema_output_formats(tmp_path) -> None:
    runner = RealClaudeRunner()
    execution = runner._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j", resume=None,
    )
    review = runner._review_options(cwd=str(tmp_path), model=None, job_id="j")
    assert execution.allowed_tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert {"Task", "WebFetch", "WebSearch", "NotebookEdit"} <= set(execution.disallowed_tools)
    assert review.allowed_tools == ["Read", "Grep", "Glob"]
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch"} <= set(review.disallowed_tools)
    assert execution.output_format["type"] == "json_schema"
    assert execution.output_format["schema"]["properties"]["status"]["enum"] == [
        "COMPLETED", "BLOCKED", "FAILED",
    ]
    assert review.output_format["schema"]["properties"]["verdict"]["enum"] == ["PASS", "FAIL"]


def test_prompts_encode_validation_review_permissions_and_compact_finish(tmp_path) -> None:
    from codex_claude_agent_mcp.claude_runner import (
        _CONTINUE_PROMPT_TEMPLATE,
        _EXEC_SYSTEM_APPEND,
        _REVIEW_PROMPT_TEMPLATE,
        _REVIEW_SYSTEM_APPEND,
        _TASK_PROMPT_TEMPLATE,
    )

    runner = RealClaudeRunner()
    execution = runner._exec_options(
        cwd=str(tmp_path), model=None, effort=None, job_id="j", resume=None,
    )
    review = runner._review_options(cwd=str(tmp_path), model=None, job_id="j")
    for options in (execution, review):
        prompt = options.system_prompt["append"]
        assert "Finish with StructuredOutput without duplicating it" in prompt
        assert "tool is unavailable, output only this JSON" in prompt
    exec_prompt = execution.system_prompt["append"]
    compact_exec_prompt = " ".join(exec_prompt.split())
    assert "Own necessary validation for changed code" in compact_exec_prompt
    assert "reliable failing test or clear error evidence already counts" in compact_exec_prompt
    assert "rerun the same condition afterward" in compact_exec_prompt
    review_prompt = review.system_prompt["append"]
    compact_review_prompt = " ".join(review_prompt.split())
    assert "Use Read, Grep, and Glob only" in compact_review_prompt
    assert "Never use shell, external MCP tools, or write tools" in compact_review_prompt
    assert "smallest targeted check CC-1 should run" in compact_review_prompt
    assert "Run the smallest necessary validation" in _CONTINUE_PROMPT_TEMPLATE
    assert "rerun its original failing condition/test" in _CONTINUE_PROMPT_TEMPLATE
    assert "without duplicate text" in _CONTINUE_PROMPT_TEMPLATE
    assert sum(map(len, (
        _EXEC_SYSTEM_APPEND, _REVIEW_SYSTEM_APPEND, _CONTINUE_PROMPT_TEMPLATE,
        _TASK_PROMPT_TEMPLATE, _REVIEW_PROMPT_TEMPLATE,
    ))) < 2_400


async def test_mcp_tool_descriptions_stay_compact() -> None:
    import warnings

    from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning
    from codex_claude_agent_mcp.server import build_server

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", IncompleteFieldDefinitionWarning)
        tools = await build_server().list_tools()
    assert all(tool.description for tool in tools)
    assert sum(len(tool.description or "") for tool in tools) < 500


async def test_invalid_structured_execution_payload_is_failed(monkeypatch, tmp_path) -> None:
    runner = RealClaudeRunner()
    result_msg = SimpleNamespace(
        session_id="sid", total_cost_usd=None, is_error=False,
        structured_output={
            "status": "UNKNOWN", "summary": "x", "files_changed": [], "validation": [],
        },
        result=None,
    )

    async def fake_run_query(prompt, options, timeout_sec, on_session_id=None, **kwargs):
        return result_msg, "", None, None

    monkeypatch.setattr(runner, "_run_query", fake_run_query)
    result = await runner.execute(
        task="task", cwd=str(tmp_path), acceptance=[], model=None,
        effort=None, timeout_sec=5, job_id="j",
    )
    assert result.status == "FAILED"
    assert result.error is not None
    # Invalid structured results are deterministic protocol errors.
    assert result.error.code == "CLAUDE_PROTOCOL_ERROR"
    assert result.error.retryable is False


# --- _run_query stream cleanup ------------------------------------------------


class _SpyStream:
    """Wraps an async generator and records whether aclose() was called."""

    def __init__(self, gen) -> None:
        self._gen = gen
        self.aclose_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._gen.__anext__()

    async def aclose(self):
        self.aclose_called = True
        await self._gen.aclose()


async def _slow_query(*, prompt, options):
    await asyncio.sleep(30)
    yield None  # pragma: no cover - never reached


async def _empty_query(*, prompt, options):
    return
    yield None  # pragma: no cover - makes this an async generator


@pytest.mark.parametrize("query_impl,expect_timeout", [
    (_slow_query, True),
    (_empty_query, False),
])
async def test_run_query_closes_stream(monkeypatch, query_impl, expect_timeout) -> None:
    import claude_agent_sdk

    holder: dict = {}

    def make_spy(*, prompt, options):
        spy = _SpyStream(query_impl(prompt=prompt, options=options))
        holder["spy"] = spy
        return spy

    monkeypatch.setattr(claude_agent_sdk, "query", make_spy)

    runner = RealClaudeRunner()
    if expect_timeout:
        with pytest.raises(MCPError) as ei:
            await runner._run_query("p", options=None, timeout_sec=0)
        assert ei.value.code == TASK_TIMEOUT
    else:
        result_msg, text, session_id, stream_error = await runner._run_query("p", options=None, timeout_sec=5)
        assert result_msg is None and text == "" and session_id is None and stream_error is None
    assert holder["spy"].aclose_called, "query stream must be aclose()d on every exit path"


# --- session store row mapping ------------------------------------------------


async def test_store_row_mapping_by_name(tmp_db_path) -> None:
    """Rows must map by column name (sqlite3.Row), not by position."""
    from codex_claude_agent_mcp.session_store import SessionStore

    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        created = await store.create_job("job-1", "do thing", ["crit"], "/tmp", status="QUEUED")
        assert created["acceptance"] == ["crit"]

        got = await store.get_job("job-1")
        assert got is not None
        # every field lands on the right key regardless of column order
        assert got["job_id"] == "job-1"
        assert got["task"] == "do thing"
        assert got["acceptance"] == ["crit"]
        assert got["cwd"] == "/tmp"
        assert got["status"] == "QUEUED"
        assert got["execution_session_id"] is None
        assert got["review_session_id"] is None

        await store.update_fields("job-1", status="COMPLETED", execution_session_id="s1")
        got = await store.get_job("job-1")
        assert got["status"] == "COMPLETED"
        assert got["execution_session_id"] == "s1"
        assert got["updated_at"] >= got["created_at"]

        listed = await store.list_jobs()
        assert len(listed) == 1 and listed[0]["job_id"] == "job-1"
    finally:
        await store.close()


async def test_store_status_transition_is_compare_and_set(tmp_db_path) -> None:
    from codex_claude_agent_mcp.session_store import SessionStore

    store = SessionStore(tmp_db_path)
    await store.init()
    try:
        await store.create_job("cas-1", "task", [], "/tmp", status="COMPLETED")
        assert await store.transition_status(
            "cas-1", from_statuses={"COMPLETED"}, to_status="REVIEWING"
        )
        assert not await store.transition_status(
            "cas-1", from_statuses={"COMPLETED"}, to_status="RESUMING_EXECUTION"
        )
        job = await store.get_job("cas-1")
        assert job["status"] == "REVIEWING"
    finally:
        await store.close()
