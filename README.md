# codex-claude-agent-mcp

A local **STDIO MCP server** that lets **Codex** delegate already-scoped
coding tasks to **Claude Code** through the **Claude Agent SDK**.

Codex is the intelligent orchestrator: it decomposes large tasks, keeps the
high-judgment decisions, and emits a concrete `task` + `acceptance` package for
each delegated job. This MCP server is a **thin, deterministic job runner and
session manager** — it does not plan, does not split tasks, and does not invent
acceptance criteria.

```
Codex  (MCP client)
      │  stdin / stdout  (STDIO MCP)
      ▼
codex-claude-agent-mcp  (local subprocess)
      ├── Claude execution session  (reads/edits/runs tools)
      └── Claude review session     (fresh, read-only, PASS/FAIL)
```

## Tools

| Tool           | Purpose                                                                |
| -------------- | --------------------------------------------------------------------- |
| `execute_task` | Run a scoped task via Claude Code; returns `session_id`. Never reviews.|
| `review_task`  | Fresh read-only Claude session judges PASS/FAIL. Needs `acceptance`.   |
| `continue_task`| Resume an execution session with reviewer/orchestrator feedback.       |
| `run_job`      | Execute, then optionally review, as one deterministic flow.           |
| `run_jobs`     | Batch of independently-scoped jobs with bounded concurrency.          |
| `get_job_status` | Read persisted job state (sessions, stage statuses); post-timeout recovery. |
| `ping`         | Liveness check (pid, server/SDK version, effective CLI path/version, buffer size); never touches Claude. |

Fresh execution/review sessions receive a server-generated UUID before launch.
The stream must echo it before it is marked resumable. Stage state, session
confirmation, and structured errors are persisted; `get_job_status(job_id,
cwd)` safely recovers them after a timeout or lost response.

### Job-scoped project authorization

`execute_task`, `review_task`, `run_job`, and each `run_jobs` `JobSpec` accept an
optional `project_root`. Codex passes the trusted active workspace root
explicitly — it is **never** inferred from the MCP process cwd.

- Supplied → it must be an existing absolute directory, `cwd` must be that root
  or a child of it, and it is accepted even outside `ALLOWED_PROJECT_ROOTS`.
  The resolved root is persisted with the job.
- Omitted → the legacy `ALLOWED_PROJECT_ROOTS` check applies unchanged.
- `continue_task` has no `project_root` input: it inherits the stored root (and
  always requires the persisted `cwd` and execution session).
- `review_task` on an existing job inherits the stored root; a supplied
  `project_root` must match it exactly. It can never add, switch, or widen a
  root. A review-only new job may establish one.

### Deterministic rules (spec §6, §11)

- `review=false` → execute only.
- `review=true` + non-empty `acceptance` → execute → fresh review → combined result.
- `review=true` + **empty** `acceptance` → `REVIEW_REQUIRES_ACCEPTANCE` error (before any Claude call).
- The MCP **never** generates acceptance criteria, never splits tasks, and never
  decides on its own whether to review.
- Execution owns proportionate validation and unclear-Bug reproduction/retest;
  existing failing tests or clear error evidence already count as reproduction.
- Fresh review is Read/Grep/Glob-only and checks code/logic plus the reported
  evidence; when evidence is insufficient it names the targeted check CC-1 needs.

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo>
cd codex-claude-agent-mcp
uv sync
```

Run the test suite (uses a fake runner — no Claude API calls):

```bash
uv run pytest
```

## Codex STDIO MCP configuration (Windows)

Add one of the following to your Codex MCP config. Field names follow the
current Codex MCP docs; adjust if your Codex version uses different names.

### Option A — use the project's venv directly (recommended on Windows)

```toml
[mcp_servers.codex-claude-agent]
command = 'D:\path\to\codex-claude-agent-mcp\.venv\Scripts\python.exe'
args = ['-m', 'codex_claude_agent_mcp']
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
CLAUDE_MAX_CONCURRENCY = '4'
# Optional long-term trust list; leave empty to rely on per-job project_root.
ALLOWED_PROJECT_ROOTS = 'D:\projects'
```

### Option B — installed console script

```bash
uv tool install .          # or: pipx install .
```

```toml
[mcp_servers.codex-claude-agent]
command = "codex-claude-agent-mcp"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
ALLOWED_PROJECT_ROOTS = 'D:\projects'
```

> `ALLOWED_PROJECT_ROOTS` is **optional**: it is a long-term trust list, not a
> startup requirement. Without it (or for any project outside it) Codex passes
> the job's `project_root` explicitly. Windows uses `;` between roots.

## Configuration (environment variables)

| Variable                       | Default                         | Meaning                                              |
| ------------------------------ | ------------------------------- | --------------------------------------------------- |
| `CLAUDE_MAX_CONCURRENCY`       | `4`                             | Max concurrent Claude sessions **per MCP process**. |
| `DEFAULT_TASK_TIMEOUT_SEC`     | `3600`                          | Default per-task timeout.                           |
| `MAX_TASK_TIMEOUT_SEC`         | `7200`                          | Hard ceiling for `timeout_sec`.                     |
| `CODEX_CLAUDE_AGENT_MCP_DB_PATH`     | `~/.codex-claude-agent-mcp/state.db`  | SQLite state path (shared across processes).        |
| `ALLOWED_PROJECT_ROOTS`        | *(empty = none)*                | Optional `;`-separated (Windows) / `:`-separated (POSIX) long-term `cwd` roots. Empty grants nothing; per-job `project_root` authorizes the rest. |
| `ALLOWED_MODELS`               | *(empty = allow any)*           | Comma-separated allowed model overrides.            |
| `CLAUDE_CLI_PATH`              | *(bundled)*                     | Path to the Claude Code CLI executable.             |
| `CLAUDE_SDK_MAX_BUFFER_SIZE`   | `20971520` (20 MiB)             | Max bytes buffered per streamed SDK JSON line (default SDK limit is 1 MiB, which large tool_result payloads can exceed). |
| `CODEX_CLAUDE_AGENT_MCP_LOG_LEVEL`   | `INFO`                          | stderr log level.                                   |

### Concurrency caveat (spec §13.2)

The concurrency limit is **per MCP server process**. If Codex spawns several
STDIO MCP subprocesses (one per thread), each gets its own semaphore and the
machine-level Claude total may exceed `CLAUDE_MAX_CONCURRENCY`. Claude API rate
limits still apply. A global cross-process limit is an explicit non-goal for v1.

## stdout / stderr discipline (spec §5)

- **stdout** carries **only** MCP protocol messages (newline-delimited JSON-RPC).
- All logs, diagnostics, tracebacks, and startup messages go to **stderr**.
- Claude SDK subprocess output is captured by the runner and returned as
  structured tool results — it is never piped raw to stdout.

## Process model (spec §4)

- One launched MCP server handles **many sequential tool calls** over the same
  stdin/stdout connection. It is **not** restarted per call.
- Multiple Codex threads may produce multiple independent MCP subprocesses. The
  implementation remains correct when several run at once (multi-process-safe
  SQLite: WAL, `busy_timeout`, `BEGIN IMMEDIATE`, `UNIQUE(job_id)`).
- Review and resume operations atomically claim their job state, preventing two
  MCP processes from mutating the same job lifecycle at once.
- A cancelled client request becomes stage `INCOMPLETE` with a persisted
  `CANCELLED` error instead of remaining in an active state.
- Claude session subprocesses are separate from MCP server processes and do not
  count toward "MCP server" count.

## Architecture

```
src/codex_claude_agent_mcp/
├── __init__.py        # version + main() entrypoint
├── __main__.py        # `python -m codex_claude_agent_mcp`
├── server.py          # FastMCP server, 7 tools, core logic
├── config.py          # env config + cwd/model/timeout validation
├── models.py          # Pydantic tool input/result models
├── scheduler.py       # per-process asyncio.Semaphore
├── claude_runner.py   # ClaudeRunner ABC + RealClaudeRunner + FakeClaudeRunner
├── session_store.py   # multi-process-safe SQLite store
├── errors.py          # structured error codes + MCPError
└── logging.py         # stderr logger with pid marker
```

The server depends only on the `ClaudeRunner` ABC, so tests use
`FakeClaudeRunner` and never call the real Claude API.

## Error model (spec §18)

Every result carries an optional `error` field (`null` on success). Errors are
structured, never raw tracebacks:

```json
{"code": "REVIEW_REQUIRES_ACCEPTANCE", "message": "...", "retryable": false, "details": {}}
```

Codes: `INVALID_ARGUMENT`, `REVIEW_REQUIRES_ACCEPTANCE`, `INVALID_CWD`,
`CLAUDE_SESSION_ERROR`, `CLAUDE_AUTH_ERROR`, `CLAUDE_BILLING_ERROR`,
`CLAUDE_PROVIDER_ERROR`, `CLAUDE_PROTOCOL_ERROR`, `RATE_LIMITED`, `TASK_TIMEOUT`,
`SESSION_NOT_FOUND`, `SESSION_MISMATCH`, `JOB_CONTEXT_MISMATCH`,
`JOB_STATE_CONFLICT`, `CANCELLED`, `STATE_STORE_ERROR`, `INTERNAL_ERROR`.

## Safety invariants

- A persisted `job_id` is permanently bound to its original task, acceptance
  criteria, resolved working directory, and (when supplied) its `project_root`.
  Review rejects any mismatch; the root is not writable through the store.
- `continue_task` resumes only the execution session stored for that exact job
  after Claude confirmed it, and inherits that job's `project_root`; it has no
  root input of its own.
- A review of an existing job can only inherit or exactly match the stored root.
  Only a review-only new job may establish one.
- Execution is limited to Read/Grep/Glob/Edit/Write/Bash; web access, nested
  agents, and notebook editing are denied. Review remains read-only.
- `project_root` / `ALLOWED_PROJECT_ROOTS` are `cwd`/start-directory gates. They
  are not an operating-system sandbox: execution Bash commands still run with the
  MCP process account's permissions. Use narrow roots and run only on trusted code.
- Claude result payloads use Agent SDK JSON Schema structured output and are
  validated before being returned to Codex.

## Optional Codex orchestration skill

The repository includes `skills/codex-sol-claude-orchestrator/SKILL.md`.
Copy that directory to `~/.codex/skills/` if you want Codex to apply the
delegation workflow automatically. It is intentionally separate from MCP
server installation.

### Other MCP clients

The server is not tied to the Codex executable: another agent can use it if its
client supports local STDIO MCP and structured tool results. That client must
provide a trusted `project_root` for each new job (or rely on explicitly
configured `ALLOWED_PROJECT_ROOTS`) and must orchestrate task/review/resume
calls itself. The bundled orchestration Skill is Codex-specific and is optional.

## Status: v0.3 hardened

See `CODEX_CLAUDE_AGENT_MCP_SPEC.md` §25 for the full checklist. All v1 items are
implemented and covered by deterministic tests (`tests/`), including session
recovery, provider/protocol classification, lifecycle, concurrency, migrations,
and STDIO protocol cleanliness. Tests never call the paid Claude API.

## License

Licensed under the [Non-Commercial Reciprocal Source License 1.0](LICENSE):
non-commercial use only, attribution required, and distributed, derived, or
network-served Covered Works must publish their Corresponding Source under the
same license. Commercial use requires separate prior written permission.

Because commercial use is prohibited, this is a **source-available** license,
not an OSI-approved open-source license.

## Official references

- MCP Specification: https://modelcontextprotocol.io/specification/
- MCP Python SDK: https://py.sdk.modelcontextprotocol.io/
- Claude Agent SDK: https://code.claude.com/docs/en/agent-sdk/overview
- OpenAI Codex MCP: https://developers.openai.com/codex/mcp
