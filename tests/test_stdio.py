"""STDIO protocol tests (spec §23.2).

Spawn the real MCP server as a subprocess and speak newline-delimited JSON-RPC
over its stdin/stdout (the framing the MCP STDIO transport actually uses).

Verifies:
1. stdout contains ONLY MCP JSON messages (every line parses as JSON).
2. stderr carries the logs.
3. One server process handles multiple sequential tool calls without restart.
4. Claude output never pollutes stdout (a FakeClaudeRunner is injected so no
   real Claude subprocess is spawned; the same stdout discipline applies).

The server is launched via a shim that injects FakeClaudeRunner into AppState.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SHIM = textwrap.dedent("""
    import asyncio
    import os
    from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner
    from codex_claude_agent_mcp import server as S

    _app = None
    async def _get_app():
        global _app
        if _app is None:
            _app = await S.AppState.create(runner=FakeClaudeRunner(execute_mode=os.environ.get('TEST_FAKE_MODE', 'success')))
        return _app
    S.get_app = _get_app

    S.configure_logging()
    mcp = S.build_server()
    mcp.run(transport="stdio")
""")


_UNSET = object()


def _launch(tmp_path: Path, allowed_roots: object = _UNSET, fake_mode: str = 'success') -> subprocess.Popen:
    env = dict(os.environ)
    env["CODEX_CLAUDE_AGENT_MCP_DB_PATH"] = str(tmp_path / "stdio.db")
    env["CODEX_CLAUDE_AGENT_MCP_LOG_LEVEL"] = "DEBUG"
    if allowed_roots is _UNSET:
        env["ALLOWED_PROJECT_ROOTS"] = str(Path.cwd())
    elif allowed_roots is None:
        env.pop("ALLOWED_PROJECT_ROOTS", None)  # optional: start with no long-term roots
    else:
        env["ALLOWED_PROJECT_ROOTS"] = str(allowed_roots)
    env["PYTHONUNBUFFERED"] = "1"
    env["TEST_FAKE_MODE"] = fake_mode
    return subprocess.Popen(
        [sys.executable, "-c", _SHIM],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _write(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen, timeout: float = 15.0) -> dict:
    """Read one JSON message line from stdout."""
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"stdout closed prematurely. stderr={err[:3000]}")
    return json.loads(line)


def _read_response(proc: subprocess.Popen, expected_id, timeout: float = 15.0) -> dict:
    """Read lines until a JSON-RPC response with the expected id arrives."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = _read(proc, timeout=deadline - time.monotonic())
        if msg.get("id") == expected_id:
            return msg
        # else it's a notification; keep reading
    raise AssertionError(f"timed out waiting for response id={expected_id}")


def _initialize(proc: subprocess.Popen) -> dict:
    _write(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    })
    resp = _read_response(proc, 1)
    _write(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return resp


def _call(proc: subprocess.Popen, req_id: int, name: str, args: dict) -> dict:
    _write(proc, {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                  "params": {"name": name, "arguments": args}})
    return _read_response(proc, req_id)


def _drain_stdout(proc: subprocess.Popen) -> list[str]:
    """After stdin is closed, read any remaining stdout lines."""
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = proc.stdout.read() if proc.stdout else ""
    return [l for l in out.splitlines() if l.strip()]


def test_background_stdio_ack_status_schema_and_shutdown(tmp_path):
    import sqlite3
    import time
    proc = _launch(tmp_path, fake_mode='slow')
    try:
        _initialize(proc)
        _write(proc, {'jsonrpc':'2.0','id':80,'method':'tools/list','params':{}})
        tools = {t['name']:t for t in _read_response(proc,80)['result']['tools']}
        assert tools['execute_task']['inputSchema']['properties']['background']['default'] is False
        assert 'background' not in tools['run_job']['inputSchema']['properties']
        started=time.monotonic()
        result=_call(proc,81,'execute_task',{'job_id':'async-stdio','task':'t',
            'cwd':str(Path.cwd()),'background':True})['result']['structuredContent']
        assert time.monotonic()-started < 5  # Fake slow runner waits 30 seconds.
        assert result['status']=='QUEUED' and not result['execution_completed']
        status=_call(proc,82,'get_job_status',{'job_id':'async-stdio',
            'cwd':str(Path.cwd())})['result']['structuredContent']
        assert status['status'] in {'QUEUED','EXECUTING'}
        _call(proc,83,'ping',{})  # Same process is responsive during execution.
        for line in _drain_stdout(proc): json.loads(line)
        assert proc.returncode == 0
        with sqlite3.connect(tmp_path/'stdio.db') as conn:
            assert conn.execute("SELECT status FROM jobs WHERE job_id='async-stdio'").fetchone()[0]=='EXECUTION_INCOMPLETE'
        assert 'Task exception was never retrieved' not in proc.stderr.read()
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()


def test_background_stdio_final_result(tmp_path):
    proc=_launch(tmp_path)
    try:
        _initialize(proc)
        _call(proc,90,'execute_task',{'job_id':'bg-done','task':'t','cwd':str(Path.cwd()),'background':True})
        for req in range(91,111):
            status=_call(proc,req,'get_job_status',{'job_id':'bg-done','cwd':str(Path.cwd())})['result']['structuredContent']
            if status['status']=='COMPLETED': break
        assert status['status']=='COMPLETED'
        assert status['execution_result']['validation']==['fake test passed']
    finally:
        proc.stdin.close(); proc.wait(timeout=10)


def test_stdout_is_only_mcp_protocol(tmp_path):
    """stdout must contain only MCP JSON; every line parses; logs are on stderr."""
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        _call(proc, 2, "ping", {})
        # execute_task triggers AppState init + our structured logging
        _call(proc, 3, "execute_task",
              {"job_id": "clean-1", "task": "do", "cwd": str(Path.cwd())})
        remaining = _drain_stdout(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    # Every remaining stdout line must be valid JSON (no stray logs/tracebacks).
    for line in remaining:
        json.loads(line)  # raises if not JSON
    # Our structured logs (with pid marker) must be on stderr, not stdout.
    err = proc.stderr.read() if proc.stderr else ""
    assert "[pid=" in err
    assert "session store ready" in err
    assert "execute_tool" in err


def test_multiple_tool_calls_one_process(tmp_path):
    """One server handles execute -> review -> continue without restart."""
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        r1 = _call(proc, 10, "execute_task",
                   {"job_id": "stdio-1", "task": "do", "acceptance": ["c"],
                    "cwd": str(Path.cwd())})
        exec_sc = r1["result"]["structuredContent"]
        assert exec_sc["status"] == "COMPLETED"
        sid = exec_sc["execution_session_id"]
        assert sid

        r2 = _call(proc, 11, "review_task",
                   {"job_id": "stdio-1", "original_task": "do",
                    "acceptance": ["c"], "cwd": str(Path.cwd())})
        rev_sc = r2["result"]["structuredContent"]
        assert rev_sc["status"] == "PASS"
        assert rev_sc["review_session_id"] != sid  # fresh session

        r3 = _call(proc, 12, "continue_task",
                   {"job_id": "stdio-1", "execution_session_id": sid,
                    "feedback": "tweak", "cwd": str(Path.cwd())})
        cont_sc = r3["result"]["structuredContent"]
        assert cont_sc["status"] == "COMPLETED"
        assert cont_sc["execution_session_id"] == sid  # resumed same session
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_project_root_over_mcp_transport(tmp_path):
    """A job-scoped project_root outside ALLOWED_PROJECT_ROOTS works end-to-end."""
    workspace = tmp_path / "mcp-workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "mcp-elsewhere"
    elsewhere.mkdir()
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        # cwd == project_root, which is outside ALLOWED_PROJECT_ROOTS (= process cwd)
        ok = _call(proc, 40, "execute_task",
                   {"job_id": "scoped-1", "task": "do", "cwd": str(workspace),
                    "acceptance": ["c"], "project_root": str(workspace)})
        sc = ok["result"]["structuredContent"]
        assert sc["status"] == "COMPLETED", sc
        sid = sc["execution_session_id"]

        # review inherits the stored root (no project_root repeated)
        rev = _call(proc, 41, "review_task",
                    {"job_id": "scoped-1", "original_task": "do", "acceptance": ["c"],
                     "cwd": str(workspace)})
        assert rev["result"]["structuredContent"]["status"] == "PASS"

        # continue has no project_root field and inherits the stored root
        cont = _call(proc, 42, "continue_task",
                     {"job_id": "scoped-1", "execution_session_id": sid,
                      "feedback": "tweak", "cwd": str(workspace)})
        assert cont["result"]["structuredContent"]["status"] == "COMPLETED"

        # a cwd outside the supplied root is rejected
        bad = _call(proc, 43, "execute_task",
                    {"job_id": "scoped-2", "task": "do", "cwd": str(elsewhere),
                     "project_root": str(workspace)})
        assert bad["result"]["structuredContent"]["error"]["code"] == "INVALID_CWD"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_starts_without_allowed_project_roots(tmp_path):
    """Empty ALLOWED_PROJECT_ROOTS: the server starts but grants no directory."""
    workspace = tmp_path / "no-root-workspace"
    workspace.mkdir()
    proc = _launch(tmp_path, allowed_roots=None)
    try:
        _initialize(proc)
        assert _call(proc, 50, "ping", {})["result"]["structuredContent"]["ok"] is True

        # No project_root and no long-term root -> nothing is authorized.
        denied = _call(proc, 51, "execute_task",
                       {"job_id": "noroot-1", "task": "do", "cwd": str(workspace)})
        assert denied["result"]["structuredContent"]["error"]["code"] == "INVALID_CWD"

        # An explicit job-scoped root still works.
        ok = _call(proc, 52, "execute_task",
                   {"job_id": "noroot-2", "task": "do", "cwd": str(workspace),
                    "project_root": str(workspace)})
        assert ok["result"]["structuredContent"]["status"] == "COMPLETED"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_review_requires_acceptance_before_claude(tmp_path):
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        resp = _call(proc, 20, "run_job",
                     {"job_id": "stdio-bad", "task": "t", "cwd": str(Path.cwd()),
                      "review": True, "acceptance": []})
        sc = resp["result"]["structuredContent"]
        assert sc["execution"]["error"]["code"] == "REVIEW_REQUIRES_ACCEPTANCE"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_stderr_carries_logs(tmp_path):
    """Our structured stderr logger fires during a real tool call."""
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        _call(proc, 30, "execute_task",
              {"job_id": "log-1", "task": "do", "cwd": str(Path.cwd())})
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    err = proc.stderr.read() if proc.stderr else ""
    assert err  # non-empty
    assert "[pid=" in err  # structured pid marker
    assert "session store ready" in err
    assert "execute_tool" in err
    # secrets must never appear
    assert "sk-" not in err


def test_no_restart_between_calls_pid_stable(tmp_path):
    """The server pid must not change across tool calls (no per-call respawn)."""
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        pids = set()
        for i in range(4, 8):
            r = _call(proc, i, "ping", {})
            pids.add(r["result"]["structuredContent"]["pid"])
        assert len(pids) == 1  # same process throughout
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_tool_schema_and_ping_metadata(tmp_path):
    proc = _launch(tmp_path)
    try:
        _initialize(proc)
        _write(proc, {"jsonrpc": "2.0", "id": 70, "method": "tools/list", "params": {}})
        tools = _read_response(proc, 70)["result"]["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        assert len(by_name) == 7
        assert {"ping", "execute_task", "review_task", "continue_task",
                "run_job", "run_jobs", "get_job_status"} == set(by_name)
        required = set(by_name["get_job_status"]["inputSchema"]["required"])
        assert {"job_id", "cwd"} <= required

        ping = _call(proc, 71, "ping", {})["result"]["structuredContent"]
        assert ping["version"] == "0.4.0"
        assert ping["sdk_version"]
        assert ping["cli_version"]
        assert ping["max_buffer_size"] == 20 * 1024 * 1024
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
