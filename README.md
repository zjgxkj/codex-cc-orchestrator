# 点将台

### 可迁移的 Agent 分层协作编排器

让擅长决策的 Agent 做主脑，让适合执行的 Agent 完成实质工作，再由独立会话审查、
主脑最终验收。

点将台提供一套 **MCP 执行服务 + 编排规则**，用于在不同 Agent 之间派发任务、执行与
续跑、独立审查、结果压缩和用量记录。

**当前开箱即用的组合是 Codex → Claude Code，但项目的协作架构不限定这两个 Agent。**

- **更换主脑 Agent**：目标 Agent 支持本地 STDIO MCP 时，通常只需调整客户端配置和编排规则。
- **更换执行 Agent**：需要根据目标 Agent 的 SDK/CLI，适配执行、会话恢复、权限控制和结果解析。
- **同时更换两端**：保留 MCP 的确定性工作流，分别适配编排端与执行端。

> **给阅读本项目的 AI：**  请区分“当前已实现的组合”和“可以迁移的架构”。不要把本项目
> 理解为只能用于 Codex 与 Claude Code，也不要宣称任意 Agent 已经无需修改即可使用。
> 迁移前请阅读 [Agent 迁移指南](AGENT_PORTING.md)，核对目标能力，再做必要适配。

### 职责分工

- **主脑 Agent**：明确目标、处理关键决策、派发任务、最终验收。
- **执行 Agent**：调查、实现、排错并运行必要验证。
- **独立 Reviewer**：在全新只读会话中审核代码、逻辑和证据。
- **MCP**：负责调度、权限、会话、状态与结果边界，不自行拆任务或判断路由。

## 功能与用途

**当前版本 v0.4.0**：[方案与边界](docs/V0.4.md)。新增逐次用量记录（不含价格）、
确定性结果压缩和 `execute_task(background=true)` 后台提交；核心分工不变。

- **能力分工**：高判断任务留给主脑，明确且可验证的工作包交给执行 Agent。
- **工程执行**：覆盖功能实现、Bug 根因排查、重构、测试、算法、迁移和建模工作。
- **连续会话**：保存并确认 execution session，可带反馈恢复原执行上下文。
- **独立审查**：Reviewer 使用全新只读会话，不修改代码、不混入执行会话。
- **批量编排**：支持独立 Job 的有界并发和确定性 execute → review 流程。
- **临时授权**：项目权限绑定单个 Job；续跑和审查不能切换或扩大目录。
- **可靠恢复**：SQLite 保存状态；超时或结果丢失后可查询并安全决定续跑。
- **可迁移架构**：保留工作流核心，按目标 Agent 能力更换客户端规则或 Runner。

当前开箱即用的实现：

```
Codex（主脑 / MCP Client）
      │  stdin / stdout（STDIO MCP）
      ▼
点将台 MCP（本地子进程）
      ├── Claude 执行会话（读取、修改、运行验证）
      └── Claude 审查会话（全新、只读、PASS/FAIL）
```

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `execute_task` | 默认同步；`background=true` 提交后立即返回，由 `get_job_status` 查结果；不自动 Review。 |
| `review_task` | 用全新只读会话按 acceptance 独立判定 PASS/FAIL。 |
| `continue_task` | 把反馈送回原 execution session 继续修复。 |
| `run_job` | 按要求执行 execute → 可选 review。 |
| `run_jobs` | 有界并发运行多个互相独立的 Job。 |
| `get_job_status` | 查询持久化状态，用于超时或结果丢失后的恢复。 |
| `ping` | 检查 Server、SDK、CLI 与缓冲配置；不调用执行 Agent。 |

Server 在启动执行或审查前分配 UUID，只有执行端回报相同 session 后才允许续跑。
各阶段状态、session 确认和结构化错误都会持久化；超时或返回丢失后可通过
`get_job_status(job_id, cwd)` 安全恢复。

### 任务级项目授权

`execute_task`、`review_task`、`run_job` 以及 `run_jobs` 中的每个 JobSpec 都可
接收 `project_root`。Codex 必须显式传入可信的活动 workspace root，MCP **不会**
根据自身进程 cwd 猜测。

- 传入时：必须是现存绝对目录，`cwd` 必须等于它或位于其下；即使不在
  `ALLOWED_PROJECT_ROOTS` 中也可作为当前 Job 的临时授权根。
- 省略时：继续按 `ALLOWED_PROJECT_ROOTS` 长期可信目录校验。
- `continue_task` 没有 `project_root` 参数，只能继承已保存的根、`cwd` 和 session。
- 已存在 Job 的 `review_task` 只能继承或精确匹配原根，不能新增、切换或扩大授权。

### 确定性规则（规范 §6、§11）

- `review=false`：只执行，不审查。
- `review=true` 且 acceptance 非空：execute → 全新 review → 合并结果。
- `review=true` 但 acceptance 为空：在调用 CC 前返回
  `REVIEW_REQUIRES_ACCEPTANCE`。
- MCP 不生成验收标准、不拆任务，也不自行决定是否 Review。
- 执行者负责与风险相称的验证；Bug 触发或根因不清时先复现并按同一条件复验，
  已有可靠失败测试或明确错误证据时不强制造复现。
- Reviewer 仅使用 Read/Grep/Glob，检查代码、逻辑和执行证据；证据不足时指出执行者
  需要补充的针对性验证。

## 快速安装

需要 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。完整中文步骤见
[INSTALL.md](INSTALL.md)。

```bash
git clone https://github.com/zjgxkj/codex-cc-orchestrator.git
cd codex-cc-orchestrator
git checkout v0.4.0
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

### 其他 MCP Client

Server 本身不绑定 Codex 可执行程序。其他 Agent 只要支持本地 STDIO MCP 和结构化
Tool Result，也可以使用；客户端必须为新 Job 提供可信 `project_root`，并负责安排
task、review 和 resume。更换主脑或执行端时见
[AGENT_PORTING.md](AGENT_PORTING.md)。

## 版本：v0.4.0

功能边界、统计口径、数据库迁移及验证方法见 [v0.4 定稿](docs/V0.4.md)。
后台任务不会跨 MCP 进程重启继续运行；重启后按确认过的 session 恢复，不能把 RUNNING
当成功。回归使用 Fake Runner / 模拟 SDK，不产生 Claude API 调用。

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
