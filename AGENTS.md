# AGENTS.md

## Project purpose

This repository, **点将台**, implements a local **STDIO MCP server** that allows Codex to
delegate already-scoped coding tasks to Claude Code through the Claude Agent SDK.

Read `docs/CODEX_CLAUDE_AGENT_MCP_SPEC.md`
before making architectural changes.

## Non-negotiable architecture

- Codex is the intelligent orchestrator.
- This MCP server is a thin, deterministic job runner and session manager.
- Do not add semantic task decomposition inside the MCP server.
- Do not let the MCP server invent acceptance criteria.
- Task, acceptance criteria, resolved cwd, and execution session must stay bound
  to the same `job_id`; reject review/resume mismatches.
- Execution and review are different Claude sessions.
- A reviewer must not resume the execution session.
- A reviewer must not modify the project under review (read-only by default).
- A failed review may be sent back to the original execution session via resume.
- `review=true` with empty acceptance criteria is an input error.
- `review=false` must never trigger a hidden review call.
- Do not silently add Claude calls that the caller did not request.

## STDIO transport

- Use STDIO MCP transport only. No HTTP server.
- A launched MCP server process must handle multiple tool calls over the same
  stdin/stdout connection.
- Do not start a new MCP server for each tool call.
- Do not assume there is globally only one MCP server process across all Codex
  threads/sessions.
- The implementation must remain correct when several copies run at once.

## stdout/stderr discipline (mandatory)

- stdout is reserved exclusively for MCP protocol messages (newline-delimited
  JSON-RPC).
- logs, diagnostics, startup messages, warnings, and tracebacks go to stderr.
- never use ordinary `print()` to stdout for debugging.
- never pipe raw Claude CLI output directly to MCP stdout.
- capture Claude results and return them as structured MCP tool results.

## Process model

- One started STDIO MCP server process may serve many tool calls during its
  connection lifetime.
- Claude Agent SDK sessions are separate execution sessions and may create their
  own Claude CLI subprocesses.
- Concurrency inside one MCP server instance is controlled with an
  application-level async semaphore/queue (`scheduler.py`).
- Cross-process global concurrency is NOT guaranteed by a per-process semaphore.

## Claude Agent SDK

- Use the current supported `query()` API (`claude_agent_sdk.query`).
- Explicitly use the Claude Code system prompt preset for coding execution:
  `system_prompt={"type": "preset", "preset": "claude_code", "append": ...}`.
- Preserve and return execution `session_id`.
- Use SDK `resume=<session_id>` for `continue_task`.
- Create a fresh session for review (no `resume`).
- Configure permissions explicitly via `allowed_tools` / `disallowed_tools` /
  `permission_mode`. Do not default to `bypassPermissions`.
- Use Agent SDK JSON Schema structured output and validate the returned payload.
- Execution: read/search/edit/bash tools allowed; it owns necessary validation.
  For unclear Bugs it reproduces before editing and retests the same condition;
  a reliable failing test or clear error evidence already counts. Review stays
  Read/Grep/Glob-only and names missing targeted evidence instead of running it.

## API behavior

Core tools: `execute_task`, `review_task`, `continue_task`, `run_job`,
`run_jobs`, and `get_job_status`, plus `ping` for liveness. Keep inputs and outputs structured
(Pydantic models). Do not return full transcripts to Codex.

## Review behavior

Reviewer instructions require independent inspection. Reviewer result is one of
`PASS` / `FAIL` / `FAILED`. On `FAIL`, return unmet criteria and concrete
evidence. Review checks code/logic and reported validation without repairing or
running tests; insufficient evidence is a concrete FAIL for CC-1 to address.

## State

Use explicit state transitions. Persist at least `job_id`,
`execution_session_id`, `review_session_id`, `cwd`, `project_root`, `status`,
timestamps in SQLite (`session_store.py`); schema changes must stay additively
backward compatible for existing databases. Multiple MCP server processes may exist, so DB
access must tolerate multiple processes: WAL, `busy_timeout`, `BEGIN IMMEDIATE`,
`UNIQUE(job_id)`. Review/resume state claims must use atomic compare-and-set.
Client cancellation must leave the affected stage as `INCOMPLETE`, persist the
aggregate `EXECUTION_INCOMPLETE` or `REVIEW_INCOMPLETE`, and attach a structured
`CANCELLED` error. A preallocated session becomes resumable only after the SDK
stream confirms the same session id.

## Concurrency

- One Claude concurrency limit per MCP server process in v1.
- Execution and review both count against that limit.
- Queue excess jobs rather than starting nested MCP servers.
- Do not attempt semantic file-conflict resolution in v1.
- Upstream Codex must not parallelize overlapping write tasks.

## Security

- Accept an optional explicit `project_root` (`execute_task`, `review_task`,
  `run_job`, each `run_jobs` `JobSpec`) supplied by upstream Codex. Never infer
  it from the MCP process cwd. When supplied it must be an existing absolute
  directory, `cwd` must be that root or a child of it, and it is accepted even
  outside `ALLOWED_PROJECT_ROOTS`; persist it with the job.
- `ALLOWED_PROJECT_ROOTS` is an optional long-term trust list, not a startup
  requirement; an empty value grants nothing (never a drive-wide fallback) and
  is used only when a call omits `project_root`.
- `continue_task` must have no way to enlarge authorization: it inherits the
  stored `project_root` and still requires the persisted cwd and session.
  `review_task` for an existing job inherits the stored root, and an explicitly
  supplied root must match it instead of expanding or switching it. Only a
  review-only new job may establish a root.
- Reject directory traversal or unauthorized roots.
- Do not log API keys, auth headers, or secret environment variables.
- Reviewer must not receive write/edit capability by default.
- A single task failure must never terminate the MCP server.

## Testing

Do not rely only on live Claude calls. `ClaudeRunner` is an ABC with a
`FakeClaudeRunner` for deterministic tests. Tests cover: invalid schema, missing
acceptance when review is requested, per-process concurrency limits,
success/failure/blocked transitions, review pass/fail, resume lookup, invalid
cwd, multiple jobs, service staying alive after a worker failure, stdout
protocol cleanliness, and two independent MCP server processes using the same
local metadata store safely.

## Scope control

Do not add these unless explicitly requested: HTTP transport, web dashboard,
Redis/Postgres, distributed workers, Kubernetes, automatic Git worktree merge,
automatic infinite repair loops, autonomous model routing inside the MCP server,
custom LLM evaluator, tool-by-tool proxying of Claude Code as the main
architecture.

Prefer the smallest implementation that satisfies the spec.

## Before declaring completion

- Run the full test suite (`uv run python -m pytest`).
- Verify one started STDIO server accepts multiple sequential tool calls without
  restarting (`tests/test_stdio.py`).
- Verify stdout contains only MCP protocol output.
- Verify logs go to stderr.
- Verify `review=true` without acceptance fails before any Claude call.
- Verify review uses a session different from execution.
- Verify `continue_task` resumes the original execution session.
- Verify two separately launched MCP server processes can coexist without
  corrupting state (`tests/test_multi_process_store.py`).
