# Codex 部署说明（Claude-Agent MCP STDIO）

> 目标：把本项目作为 STDIO MCP Server 接入 Codex，让 Codex 把已拆好的任务委派给 Claude Code 执行与验收。
>
> 适用：Windows + Codex CLI（已在 codex-cli 0.144.6 验证）。POSIX 步骤一致，仅路径分隔符不同。

> ### 🚀 快通道：本机已就绪
> 如果项目已在本地、`.venv` 已建好且依赖齐全，**第 1~2 步可跳过，直接从[第 3 步：写入 Codex 配置](#3-写入-codex-配置)开始**。
>
> 自检一句即可（不报错就行）：
> ```bash
> .venv/Scripts/python.exe -c "import mcp, claude_agent_sdk"
> ```
>
> 运行时既不需要 `uv`（它只是当初建 `.venv` 的工具），也不需要再建 venv。Codex 的 `command` 直接指向现成的 `.venv\Scripts\python.exe`。

---

## 0. 前置条件

| 依赖 | 要求 | 检查命令 |
| --- | --- | --- |
| Python | 3.13+ | `python --version` |
| uv | 任意近期版本 | `uv --version` |
| Claude Code CLI | 已登录 | `claude auth status`（`loggedIn: true`） |
| Codex CLI | 已安装并配置好 | `codex --version` |

> **Claude 鉴权**：`execute_task` / `review_task` 等真正调用 Claude 的工具需要 Claude 鉴权。只要 `claude` CLI 处于登录状态（OAuth 或 API Key），Claude Agent SDK 会自动复用，无需额外配置 `ANTHROPIC_API_KEY`。`ping` 工具不需要鉴权，可随时用来测连通性。

---

## 1. 获取代码并安装依赖

```bash
# 如果你还没拿到代码
git clone <repo-url> codex-claude-agent-mcp
cd codex-claude-agent-mcp

# 创建虚拟环境并安装依赖（mcp + claude-agent-sdk）
uv sync
```

安装完成后会在项目下生成 `.venv\`。

---

## 2. 先自测：Server 能不能起来

不接 Codex，先手动喂一条 MCP 消息验证 Server 正常：

```powershell
# Windows PowerShell；这里授权项目所在目录（可选，仅作长期信任根）
$env:ALLOWED_PROJECT_ROOTS = 'D:\projects'
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ping","arguments":{}}}
'@ | .\.venv\Scripts\python.exe -m codex_claude_agent_mcp
```

正常应看到两行 JSON 响应：第一行是 `initialize` 结果，第二行 `ping` 结果里含 `"ok": true` 和 `pid`。

跑测试套件（用 fake runner，不调用真实 Claude）：

```bash
.venv/Scripts/python.exe -m pytest
# 期望：全部通过
```

> ⚠️ **不要用 `uv run pytest`** 跑测试。本机上 `uv run` 每次会重新同步环境，经常耗时 30–120 秒甚至超时。直接用 `.venv\Scripts\python.exe -m pytest`，几秒内跑完。

---

## 3. 写入 Codex 配置

编辑 Codex 配置文件：

- Windows：`C:\Users\<你>\.codex\config.toml`
- POSIX：`~/.codex/config.toml`

在 `[mcp_servers]` 下新增一段。

### 推荐方案：直接用 venv 的 Python（启动最快、最稳）

```toml
[mcp_servers.codex-claude-agent]
command = 'D:\path\to\codex-claude-agent-mcp\.venv\Scripts\python.exe'
args = ['-m', 'codex_claude_agent_mcp']
startup_timeout_sec = 30
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
CLAUDE_MAX_CONCURRENCY = '4'
# 可选长期信任根；留空/不写也能启动，此时每次调用由 project_root 授权
ALLOWED_PROJECT_ROOTS = 'D:\projects'
# CODEX_CLAUDE_AGENT_MCP_DB_PATH = 'D:\path\to\state.db'  # 可选：默认 ~/.codex-claude-agent-mcp/state.db
# CODEX_CLAUDE_AGENT_MCP_LOG_LEVEL = 'INFO'          # 可选：DEBUG/INFO/WARNING
```

把 `D:\path\to\codex-claude-agent-mcp` 换成你机器上的真实绝对路径。

### 备选方案：用 `uv run`（可移植，但启动慢）

```toml
[mcp_servers.codex-claude-agent]
command = 'uv'
args = ['--directory', 'D:\path\to\codex-claude-agent-mcp', 'run', 'python', '-m', 'codex_claude_agent_mcp']
startup_timeout_sec = 120
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
CLAUDE_MAX_CONCURRENCY = '4'
# 可选长期信任根
ALLOWED_PROJECT_ROOTS = 'D:\projects'
```

> 用 `uv run` 时把 `startup_timeout_sec` 调到 120，给它留足环境同步时间。

### TOML 路径转义注意

- Windows 路径含反斜杠，**必须用单引号字符串**（`'...'`，TOML 字面量字符串，不转义），与上面写法一致。
- 如果用双引号 `"..."`，反斜杠会被当成转义符导致解析失败。可参考 Codex 自带的 `[mcp_servers.node_repl]` 段，它也用单引号。

---

## 4. 在 Codex 里验证

1. **重启 Codex**（或重载 MCP），让新配置生效。
2. 让 Codex 列出可用工具，应能看到：
   `ping`、`execute_task`、`review_task`、`continue_task`、`run_job`、`run_jobs`、`get_job_status`
3. 先调一次 `ping` 确认链路：
   ```
   ping()
   ```
   返回 `{"ok": true, "pid": ..., "version": "0.3.0", ...}` 即接通；同时会给出 SDK/CLI 版本和缓冲上限，不调用 Claude 服务。
4. 跑一个真实小任务验证 Claude 链路（需要 Claude 已登录）：
   ```
   run_job(
     job_id="hello-1",
     task="在项目根目录创建一个 hello.txt，内容为 hello from claude",
     cwd="D:\\sandbox",
     project_root="D:\\sandbox",  # 本次任务的授权根，来自 Codex 自身 workspace 上下文
     acceptance=["存在 hello.txt 且内容为 hello from claude"],
     review=true
   )
   ```
   期望：`execution.status=COMPLETED`，`review.status=PASS`，`status=COMPLETED`。

### 可选：安装 Codex 编排 Skill

MCP 工具和 Skill 是两套独立机制。Skill 引导 Codex 在广泛读取源码前默认委派
合适的完整工作包，并为实质变更使用独立 CC 审核；不是强制先拆分或保证自动触发。
首次安装或更新均需复制目录内容（包括按需参考文件），不要在已存在的目录内嵌套同名目录：

```powershell
$source = Join-Path (Get-Location) 'skills\codex-sol-claude-orchestrator'
$target = Join-Path $HOME '.codex\skills\codex-sol-claude-orchestrator'
New-Item -ItemType Directory -Path $target -Force | Out-Null
Get-ChildItem -LiteralPath $source | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
}
```

复制后 Codex 通常自动发现变化；未出现时重启，也可明确写 `$codex-sol-claude-orchestrator`。
只安装 MCP 不复制 Skill 也能正常手动调用七个工具。

### 其他 Agent / MCP Client

Server 本身不绑定 Codex。其他 Agent 只要支持本地 STDIO MCP 与结构化 Tool
Result，也可以使用；新 job 必须由客户端传入可信 `project_root`，或使用明确配置的
`ALLOWED_PROJECT_ROOTS`。Codex 编排 Skill 不会在其他 Agent 中自动生效，任务、
review 与 resume 的调用顺序需由其客户端负责。

---

## 5. 配置项参考（环境变量）

在 `[mcp_servers.codex-claude-agent.env]` 里设置；`ALLOWED_PROJECT_ROOTS` 可选：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLAUDE_MAX_CONCURRENCY` | `4` | **单个 MCP 进程**内 Claude 会话并发上限。 |
| `DEFAULT_TASK_TIMEOUT_SEC` | `3600` | 单任务默认超时。 |
| `MAX_TASK_TIMEOUT_SEC` | `7200` | `timeout_sec` 的硬上限。 |
| `CODEX_CLAUDE_AGENT_MCP_DB_PATH` | `~/.codex-claude-agent-mcp/state.db` | SQLite 状态库路径（多进程共享）。 |
| `ALLOWED_PROJECT_ROOTS` | 空=不授予 | 可选的**长期** `cwd` 白名单；Windows 用 `;` 分隔，POSIX 用 `:`。为空时不授予任何目录（不会退化为整盘），由每次调用的 `project_root` 授权。 |
| `ALLOWED_MODELS` | 空=允许任意 | 允许的 model 覆盖值，逗号分隔。 |
| `CLAUDE_CLI_PATH` | 内置 | 指定 Claude Code CLI 可执行文件路径。 |
| `CLAUDE_SDK_MAX_BUFFER_SIZE` | `20971520`（20 MiB） | SDK 流式 JSON 行的最大缓冲字节数（SDK 默认仅 1 MiB，大型 tool_result 会超出）。 |
| `CODEX_CLAUDE_AGENT_MCP_LOG_LEVEL` | `INFO` | stderr 日志级别。 |

> `execute_task` / `review_task` / `run_job` / `run_jobs` 的每个 JobSpec 都可带
> `project_root`：现存绝对目录，`cwd` 必须等于它或在其子目录内，允许位于
> `ALLOWED_PROJECT_ROOTS` 之外。它只授权该 job 并随 job 持久化；`continue_task`
> 没有该参数（继承已存的根），已存在 job 的 review 也只能继承或完全匹配，
> 因此续跑/验收都不会放大授权。Codex 应传自身 workspace 上下文里的可信活动
> 根目录，绝不能传 MCP 进程 cwd、猜测路径或整盘根。
>
> 授权根只限制任务允许采用的起始 `cwd`，不是 Windows 操作系统沙箱。执行
> Agent 的 Bash 仍继承 MCP 进程账户权限；只应在可信、有 Git 或备份的项目中
> 运行，并尽量把授权根设置得更窄。

### 并发语义（重要）

- `CLAUDE_MAX_CONCURRENCY` 是**每 MCP 进程**的限流，不是机器级全局上限。
- 如果 Codex 为多个 Thread 各启动一个 STDIO MCP 子进程，每个进程各有自己的上限，机器级 Claude 总并发可能超过该值。Claude API 自身限流仍然生效。
- v1 不做跨进程全局限流（不为此引入 HTTP 服务）。

---

## 6. 排错速查

| 现象 | 原因 / 处理 |
| --- | --- |
| Codex 启动该 MCP 超时 | 改用「直接 venv Python」方案；或把 `startup_timeout_sec` 调大到 120。 |
| `tool_timeout_sec` 报配置错 | 你的 Codex 版本可能不支持该字段，删掉即可（仅用于调长工具超时）。 |
| `execute_task` 报 `CLAUDE_AUTH_ERROR` | `claude` CLI 未登录，先 `claude login`，再 `claude auth status` 确认。 |
| `CLAUDE_BILLING_ERROR` / 402 | 余额或计费问题，同样请求不会靠重试恢复。 |
| 超时或返回丢失 | 调 `get_job_status(job_id, cwd)`；仅在 `execution_session_confirmed=true` 时使用 `continue_task`，`REVIEW_INCOMPLETE` 可重新 `review_task`。 |
| `INVALID_CWD` | `cwd` 不在授权根内：传了 `project_root` 时 `cwd` 必须是它或它的子目录；未传时 `cwd` 必须在 `ALLOWED_PROJECT_ROOTS` 里。 |
| MCP 启动即退出并提示 `ALLOWED_PROJECT_ROOTS contains missing directories` | 配置里写了不存在的长期根；改成本机现存绝对目录，或整行留空改用 `project_root`。 |
| `JOB_CONTEXT_MISMATCH` | 相同 `job_id` 被用于不同 task、acceptance、cwd 或 `project_root`；必须使用原任务包与原有授权根，或换新 `job_id`。 |
| `SESSION_MISMATCH` | `continue_task` 收到的 session 不属于该 job；使用该 job 执行结果返回的 session。 |
| `JOB_STATE_CONFLICT` | 同一 job 正在被另一个 MCP 进程执行、验收或恢复；等待其结束后重试。 |
| `REVIEW_REQUIRES_ACCEPTANCE` | `review=true` 但 `acceptance` 为空。MCP 不会自动补验收标准，由 Codex 提供。 |
| `STATE_STORE_ERROR: duplicate job_id` | 同一 `job_id` 重复提交。换新 id，或这是预期的并发冲突提示。 |
| stdout 里出现乱码/日志 | 不应发生。若出现，检查是否有代码用 `print()` 写 stdout；本服务所有日志只写 stderr。 |
| stderr 有一行 `IncompleteFieldDefinitionWarning ... 'lifespan'` | MCP SDK 自身设置产生的无害告警，不影响协议。可忽略。 |
| `uv run` 极慢 | 本机已知问题，`uv run` 每次重新同步环境。直接用 `.venv\Scripts\python.exe`。 |

---

## 7. 进程与协议纪律（部署后请遵守）

- **stdout 只走 MCP 协议消息**（换行分隔的 JSON-RPC）。不要给本服务加任何 `print()` 到 stdout。
- **所有日志/traceback 走 stderr**。
- 一个已启动的 MCP Server 会**连续处理多次 Tool Call**，不会每次重启。
- 多个 Codex Thread 可能产生多个独立 MCP 子进程，它们共享同一份 SQLite 状态（WAL + busy_timeout + `BEGIN IMMEDIATE` + `UNIQUE(job_id)`），互不破坏。

---

## 8. 卸载

从 `config.toml` 删除 `[mcp_servers.codex-claude-agent]` 整段并重启 Codex 即可。可选地删除 `~/.codex-claude-agent-mcp/state.db` 与项目目录。
