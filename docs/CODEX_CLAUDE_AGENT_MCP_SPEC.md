# 点将台：Claude-Agent MCP 实现规范（STDIO）

> 文档类型：Architecture + Implementation Specification  
> 受众：Codex / 实现工程师  
> 状态：v0.3 Current Implementation
> 通信模式：STDIO MCP  
> 日期：2026-08-12

---

## 1. 项目目标

实现一个本地 **STDIO MCP Server**，供 Codex 调用。

核心能力：

1. 接收 Codex 已经拆好的明确子任务。
2. 使用 Claude Agent SDK 启动 Claude Code Agent 完成任务。
3. 保存 Claude execution `session_id`，便于后续继续原会话。
4. 可使用一个**全新 Claude 会话**独立验收任务。
5. 支持一个 MCP Server 实例内多个 Job 并发，但有并发上限。
6. 同一 STDIO MCP 连接上的多次 Tool Call 复用该 Server 进程。
7. 能容忍多个 Codex Thread / Session 导致多个独立 MCP Server 子进程同时存在。
8. MCP 本身保持“薄”和确定性，不承担语义规划。

---

## 2. 非目标

第一版明确不实现：

- 不使用 Streamable HTTP。
- 不启动本地 HTTP 监听端口。
- 不让 MCP 自己理解大型需求并拆子任务。
- 不让 MCP 自己决定任务应由 Codex 还是 Claude Code 完成。
- 不让 MCP 自己创造 acceptance criteria。
- 不把 Claude Code 的 Read/Edit/Bash 单独暴露给 Codex 作为主架构。
- 不实现新的 LLM evaluator。
- 不把 Claude `/goal` 作为第一版强依赖。
- 不默认自动循环返工到无限次。
- 不默认自动创建/合并 Git worktree。
- 不依赖多 MCP 进程实现单个连接内的并发。
- 不做 Web 管理后台。

---

## 3. 总体架构

```text
Codex
（MCP Client）
      │
      │ stdin / stdout
      ▼
Claude-Agent MCP Server
（本地子进程）
      │
      ├── Claude Execution Session A
      ├── Claude Execution Session B
      └── Claude Review Session R
```

职责：

### Codex / Skill

负责：

- 大型任务理解
- Task decomposition
- 依赖判断
- 路由决策
- 为每个 Job 生成 task / scope / acceptance
- 决定 `review=true/false`
- 处理 review FAIL / BLOCKED 后的高级决策

### MCP Server

负责：

- schema 校验
- Job 排队
- 单实例内并发控制
- Claude session 启动
- Claude session resume
- execute → review 的确定性顺序
- 状态保存
- 结构化结果返回

### Claude Agent SDK / Claude Code

负责：

- 真正读代码
- 分析当前子任务
- 修改代码
- 运行工具
- 执行独立 review

---

## 4. STDIO 传输模型

### 4.1 基本模型

Codex 通过启动命令创建 MCP Server 子进程：

```text
Codex
  │
  ├── stdin  ───────→ MCP Server
  └── stdout ←─────── MCP Server
```

MCP 消息在 stdin/stdout 上按协议交换。

### 4.2 一次启动，多次 Tool Call

一个已启动的 MCP Server 必须能够连续处理：

```text
tool call #1
tool call #2
tool call #3
...
```

而不重新启动自己。

禁止：

```text
每次 tool call
  ↓
再 spawn 一个 codex-claude-agent-mcp
```

### 4.3 多 Codex Thread 时的 MCP Server 数量

**不得假设整个 Codex App 全局只存在一个 MCP Server 进程。**

STDIO Server 生命周期由 MCP Host 管理。

在多个独立 Codex Thread / Session 下，可能出现：

```text
Codex Thread A
   ↕ STDIO
MCP Process A

Codex Thread B
   ↕ STDIO
MCP Process B

Codex Thread C
   ↕ STDIO
MCP Process C
```

因此实现必须满足：

- 多份 MCP Server 同时启动不会互相破坏。
- 不依赖单进程全局内存保存唯一状态。
- 共享持久状态时使用支持多进程访问的机制。
- 不依赖“全局 Python asyncio.Semaphore”实现机器级并发限制。

### 4.4 Tool Result 返回给哪个 Thread

不由 MCP 自己做跨 Thread 路由。

每个 MCP Server 只通过自己的 stdout 返回给启动它的 MCP Client 连接。

因此：

```text
Thread A ↔ MCP Process A
Thread B ↔ MCP Process B
```

响应天然回到对应连接。

`job_id` 只用于 MCP 实例/持久状态中的 Job 识别与 execute-review 关联，不承担 Codex Thread 路由。

---

## 5. stdout / stderr 协议纪律

这是 v1 的硬性要求。

### stdout

**只能输出 MCP 协议消息。**

禁止：

```python
print("MCP started")
print(job)
print("debug")
```

如果这些内容写入 stdout，可能破坏 MCP/JSON-RPC 协议流。

### stderr

以下内容全部写 stderr：

- 日志
- debug
- trace
- warning
- traceback
- Claude runner 诊断
- 启动信息

### Claude 输出

Claude Agent SDK / CLI 的普通输出不得直接连接到 MCP stdout。

正确流程：

```text
Claude output
   ↓
Python 捕获/解析
   ↓
结构化 Tool Result
   ↓
MCP SDK 写协议响应到 stdout
```

---

## 6. 智能与非智能职责边界

### 6.1 Codex 做有智慧的事

```text
大型任务
  ↓
Codex 拆成 A / B / C
  ↓
每个 Job：
- task
- scope
- acceptance
- dependencies
- review?
```

### 6.2 MCP 只做确定性工作流

例如：

```text
Job A
├── task
├── acceptance
└── review=true

MCP：
execute A
   ↓
execute 成功
   ↓
fresh review A
```

MCP 不需要理解 acceptance 的语义归属，因为它已经与 `job_id` 绑定。

### 6.3 缺失 acceptance

规则写死：

```text
review=false + acceptance 为空
→ 合法，只执行

review=true + acceptance 为空
→ INVALID_ARGUMENT
→ REVIEW_REQUIRES_ACCEPTANCE
```

MCP **绝对不得自己生成 acceptance**。

---

## 7. 任务与验收的数据绑定

建议 Job schema：

```json
{
  "job_id": "camera-animation-fix",
  "task": "修复滚动运镜卡顿，不改变现有运动轨迹。",
  "acceptance": [
    "滚动过程无明显卡顿",
    "现有相机轨迹保持不变",
    "不修改无关模块",
    "项目仍可正常启动"
  ],
  "review": true,
  "cwd": "D:\\project"
}
```

这样：

```text
A.task
A.acceptance

B.task
B.acceptance
```

不会混淆。

---

## 8. Reviewer 会话模型

### 8.1 Execution Session

执行者：

```text
Claude Execution Session A
```

允许按照权限配置：

- Read
- Search
- Edit
- Bash
- Tests

执行者对改动负责必要验证。Bug 的触发或原因不清时先复现，修改后复验同一
条件；已有可靠失败测试或明确错误证据时不重复造复现。

Server 在调用 SDK 前预分配 session id；只有 SDK stream 回报相同 id 后才把
`execution_session_confirmed` 置为 true。未确认的 session 不得 resume。

### 8.2 Review Session

验收者：

```text
Claude Review Session R
```

必须是：

- 全新 session
- 不 resume Execution Session
- 默认只读
- 独立读取项目现状
- 根据 original_task + acceptance 判定 PASS / FAIL
- 审核代码/逻辑及执行者的验证证据；证据不足时指出 CC-1 所需的最小验证

### 8.3 Review FAIL 后返工

不要让 reviewer 修。

正确：

```text
Review FAIL
   ↓
Codex 判断是否局部返工
   ↓
continue_task(
  execution_session_id,
  reviewer_feedback
)
   ↓
恢复原 Execution Session
```

---

## 9. 技术栈

推荐：

```text
Python 3.13+
official MCP Python SDK
Claude Agent SDK for Python
asyncio
uv
SQLite
```

项目结构：

```text
codex-claude-agent-mcp/
├── pyproject.toml
├── uv.lock
├── README.md
├── AGENTS.md
├── src/
│   └── codex_claude_agent_mcp/
│       ├── __init__.py
│       ├── server.py
│       ├── config.py
│       ├── models.py
│       ├── scheduler.py
│       ├── claude_runner.py
│       ├── session_store.py
│       ├── errors.py
│       └── logging.py
└── tests/
    ├── test_schema.py
    ├── test_stdio.py
    ├── test_scheduler.py
    ├── test_lifecycle.py
    ├── test_concurrency.py
    └── test_multi_process_store.py
```

---

## 10. Claude Agent SDK

### 10.1 为什么用 Agent SDK

目标不是一次普通模型请求，而是一个完整 coding agent：

```text
任务
 ↓
Claude agent loop
 ↓
Read / Search / Edit / Bash
 ↓
继续判断
 ↓
完成
```

### 10.2 核心入口

围绕当前官方 Agent SDK 支持的 `query()` API 实现。

需要捕获：

- final result
- session_id
- success / blocked / failed
- usage/cost（若 SDK 暴露且方便）
- error

### 10.3 Claude Code system prompt preset

Coding execution 应显式使用 Claude Code preset 或当前 SDK 的官方等效配置，而不是无意间使用与 Claude Code CLI 不同的默认 prompt。

### 10.4 Execution Agent 指令

```text
你是被上游 Codex 委派的执行 Agent。

只完成给定 task。
acceptance 是完成要求，不是让你扩展需求。
不要修改无关范围。

对改动运行范围/风险相称的验证；按上述 Bug 规则复现和复验。

如果：
- 关键需求缺失
- 必须进行重大架构决策
- 实际项目与任务假设严重冲突

则返回 BLOCKED，不要擅自猜测。

结束时结构化总结：
- 修改内容
- 文件变化
- 验证
- 剩余不确定项
```

### 10.5 Reviewer 指令

```text
你是独立 reviewer。

根据：
- original_task
- acceptance
- 当前项目真实状态

判断是否完成。

不要相信执行者的“已完成”声明，必须独立检查。

不得修改项目。

只用 Read/Grep/Glob 审核代码/逻辑和已有验证证据，不运行测试；证据不足时
FAIL 并指出 CC-1 所需的最小针对性验证。

输出：
PASS
或
FAIL

FAIL 时列出：
- 未满足标准
- 证据
- 建议返工点
```

---

## 11. MCP Tool API

### 11.1 execute_task

```json
{
  "job_id": "string",
  "task": "string",
  "cwd": "string",
  "project_root": "optional; 现存绝对目录，cwd 必须在其内",
  "acceptance": ["optional"],
  "model": "optional",
  "effort": "optional",
  "timeout_sec": 3600
}
```

返回：

```json
{
  "job_id": "string",
  "status": "COMPLETED | BLOCKED | FAILED",
  "execution_completed": true,
  "execution_session_id": "string|null",
  "execution_session_confirmed": true,
  "summary": "string",
  "files_changed": [],
  "validation": [],
  "error": null
}
```

`execute_task` 永远不自动 review。

---

### 11.2 review_task

输入：

```json
{
  "job_id": "string",
  "original_task": "string",
  "acceptance": ["至少一条"],
  "cwd": "string",
  "project_root": "optional; 已存在 job 必须继承或完全匹配",
  "execution_summary": "optional"
}
```

规则：

```text
acceptance 为空
→ REVIEW_REQUIRES_ACCEPTANCE
```

输出：

```json
{
  "job_id": "string",
  "status": "PASS | FAIL | FAILED",
  "review_session_id": "string|null",
  "review_session_confirmed": true,
  "review_completed": true,
  "summary": "string",
  "unmet_criteria": [],
  "evidence": [],
  "error": null
}
```

---

### 11.3 continue_task

输入：

```json
{
  "job_id": "string",
  "execution_session_id": "string",
  "feedback": "string",
  "cwd": "string"
}
```

使用 Agent SDK resume 原 execution session。该工具没有 `project_root` 入参：
它继承 job 存储的根目录，并仍要求 cwd 与持久值一致，因此无法扩大授权。

---

### 11.4 run_job

高层工具：

```json
{
  "job_id": "string",
  "task": "string",
  "cwd": "string",
  "project_root": "optional; 该 job 的授权根",
  "acceptance": [],
  "review": true,
  "timeout_sec": 3600
}
```

确定性规则：

```text
review=false
  → execute only

review=true + acceptance 非空
  → execute
  → fresh review
  → return combined result

review=true + acceptance 为空
  → INVALID_ARGUMENT
```

MCP 不自行决定 review。

---

### 11.5 run_jobs

Codex 可以一次提交多个已经拆好的 Job：

```json
{
  "jobs": [
    {
      "job_id": "A",
      "task": "...",
      "cwd": "...",
      "project_root": "...",
      "acceptance": ["..."],
      "review": true
    },
    {
      "job_id": "B",
      "task": "...",
      "cwd": "...",
      "acceptance": [],
      "review": false
    }
  ],
  "max_concurrency": 4
}
```

规则：

- `job_id` 唯一。
- 每个 Job 自带 task/acceptance。
- A 的 review 只使用 A。
- B 的 review 只使用 B。
- MCP 不重新拆分。
- MCP 只 queue / schedule。

---

### 11.6 get_job_status

不调用 Claude，只读取持久化状态，用于超时或 Tool Result 丢失后的恢复判断：

```json
{
  "job_id": "string",
  "cwd": "string"
}
```

返回 job/stage 状态、execution/review session id 及其确认标记、摘要和结构化错误。
只有 `execution_session_confirmed=true` 的 execution session 才允许
`continue_task`；该查询仍校验 job 绑定的 `cwd` 与项目授权。

---

## 12. Job 状态机

```text
QUEUED
  ↓
EXECUTING
  ├── BLOCKED
  ├── error/cancel -> execution_status=INCOMPLETE
  │                  status=EXECUTION_INCOMPLETE
  └── EXECUTED
          │
          ├── review=false
          │      ↓
          │  COMPLETED
          │
          └── review=true
                 ↓
              REVIEWING
               ├── PASS -> COMPLETED
               ├── FAIL -> REVIEW_FAILED
               └── error/cancel -> review_status=INCOMPLETE
                                    status=REVIEW_INCOMPLETE
```

返工：

```text
REVIEW_FAILED
      ↓
Codex 调 continue_task
      ↓
RESUMING_EXECUTION
      ↓
EXECUTED
```

第一版不自动无限返工。

---

## 13. 并发模型

### 13.1 单 MCP 进程内

配置：

```text
CLAUDE_MAX_CONCURRENCY=4
```

使用：

```text
asyncio.Semaphore
```

Execution 和 Review 默认共享同一个并发池。

例如：

```text
MCP Process A
  ├── Claude A
  ├── Claude B
  ├── Claude C
  └── Claude D
```

达到上限后，新 Job 排队。

### 13.2 多 MCP 进程时

若 Codex 多 Thread 导致：

```text
MCP Process A
MCP Process B
MCP Process C
```

每个进程自己的 `asyncio.Semaphore(4)` 是**独立的**。

因此可能形成机器级总并发：

```text
A: 4
B: 4
C: 4
总计: 12
```

v1 必须明确这一点。

两种选择：

#### v1 简化方案

只做**每 MCP 进程限流**。

文档提醒用户：

- 多开 Codex Thread 会增加 Claude 并发。
- Claude API 自身 rate limit 仍然生效。
- 不承诺机器级统一上限。

#### 后续增强

如果需要全局并发上限，再实现：

- Windows named semaphore
- file lock + counter
- SQLite lease/lock
- 独立本地调度 daemon

**v1 不要为了这个重新引入 HTTP 服务。**

---

## 14. 多任务写冲突

MCP 不做语义冲突判断。

Codex 负责避免：

```text
Job A 修改 src/camera.ts
Job B 同时修改 src/camera.ts
```

v1 文档明确：

- 只读任务可大量并行。
- 不重叠模块的写任务可并行。
- 同文件/强耦合模块应串行。
- 大型并行写任务可由上游选择 Git worktree，但 MCP v1 不自动创建。

---

## 15. Session 与状态存储

至少保存：

```text
job_id
execution_session_id
execution_session_confirmed
review_session_id
review_session_confirmed
cwd
project_root
status
execution_status
review_status
execution_error
review_error
created_at
updated_at
```

`project_root` 为可空的任务级授权根；旧数据库必须通过追加列的迁移保持
向后兼容（旧行为 NULL，继续按 `ALLOWED_PROJECT_ROOTS` 校验）。

推荐 SQLite。

原因：

- MCP 进程退出后可恢复 job/session 映射。
- 多个 STDIO MCP 进程可以访问同一份持久状态。
- 无需额外网络服务。

### SQLite 多进程要求

必须正确处理：

- transactions
- busy timeout
- WAL（若测试确认适合当前实现）
- unique constraints
- duplicate job_id

不要把关键 session 映射只放在 Python dict 中。

---

## 16. 安全

### cwd

长期授权根目录（可选）：

```text
ALLOWED_PROJECT_ROOTS
```

该配置为可选的长期信任列表，为空时 Server 仍可启动，但不授予任何目录
权限（绝不退化为整盘授权），此时必须由调用方显式传入任务级根目录：

```text
project_root
```

`execute_task`、`review_task`、`run_job` 与 `run_jobs` 的每个 JobSpec 都可
选带 `project_root`，由上游 Codex 依据自身 workspace 上下文显式给出，**不得**
从 MCP 进程 cwd 推断。传入时必须是现存绝对目录，`cwd` 必须等于该目录或
其子目录，即使不在 `ALLOWED_PROJECT_ROOTS` 之下也接受，并随 job 持久化；
省略时才回落到 `ALLOWED_PROJECT_ROOTS` 检查。

每个 `cwd` 必须是现存绝对目录，并在其授权根（任务级 `project_root` 或
长期根）之下。

该检查只约束 Agent 的启动目录，不宣称是操作系统级文件系统沙箱。
Windows 上 Bash 仍继承 MCP 进程账户权限，部署文档必须明确此信任边界。

拒绝：

- 越界路径
- path traversal
- 未授权目录

### Execution 权限

按 Agent SDK 当前权限机制显式允许所需工具。

不要默认无限制 bypass。

当前版本仅允许 Read/Grep/Glob/Edit/Write/Bash；禁止 WebFetch、WebSearch、
Task、NotebookEdit 和 MultiEdit，以减少非必要权限面。

### Reviewer 权限

默认只读。

禁止：

- Edit
- Write
- NotebookEdit
- 其他明确写操作

如果 reviewer 需要执行测试命令，必须单独设计安全策略。

### Job 上下文绑定

- 已存在的 `job_id` 的 task、acceptance、解析后的 cwd、`project_root`
  不得被重新绑定。
- `review_task` 必须核对调用参数与 SQLite 持久值完全一致；已存在的 job
  只能继承存储的 `project_root`，显式传入的必须与其完全匹配，不得借此
  扩大或切换授权；只有仅做 review 的新 job 可以建立 `project_root`。
- `continue_task` 不得提供 `project_root` 入参：它继承该 job 存储的根目录，
  仍要求 cwd 与持久值一致，只能恢复该 job 保存的 execution session。
- review/resume 必须使用 SQLite compare-and-set 原子认领状态，避免多进程竞态。

---

## 17. 超时与取消

配置：

```text
DEFAULT_TASK_TIMEOUT_SEC
MAX_TASK_TIMEOUT_SEC
```

客户端取消执行、验收或恢复请求时，Server 必须在 SQLite 中把对应 stage
原子更新为 `INCOMPLETE`，聚合状态更新为 `EXECUTION_INCOMPLETE` 或
`REVIEW_INCOMPLETE`，并持久化结构化 `CANCELLED` 错误；不得遗留永久
`EXECUTING` / `REVIEWING` / `RESUMING_EXECUTION`。

单个 Claude task 超时：

```text
FAILED
TASK_TIMEOUT
```

但：

**不得因此退出 MCP Server 进程。**

---

## 18. 错误模型

统一：

```json
{
  "code": "INVALID_ARGUMENT",
  "message": "...",
  "retryable": false,
  "details": {}
}
```

至少：

```text
INVALID_ARGUMENT
REVIEW_REQUIRES_ACCEPTANCE
INVALID_CWD
CLAUDE_SESSION_ERROR
CLAUDE_AUTH_ERROR
CLAUDE_BILLING_ERROR
CLAUDE_PROVIDER_ERROR
CLAUDE_PROTOCOL_ERROR
RATE_LIMITED
TASK_TIMEOUT
SESSION_NOT_FOUND
SESSION_MISMATCH
JOB_CONTEXT_MISMATCH
JOB_STATE_CONFLICT
CANCELLED
STATE_STORE_ERROR
INTERNAL_ERROR
```

stack trace 写 stderr 日志，不直接作为 Tool Result 主体。

---

## 19. 日志

使用标准 logging 写 stderr。

字段：

```text
request_id
job_id
phase
session_id
pid
duration_ms
status
```

记录 `pid` 很重要，因为 STDIO 模式下可能同时存在多个 MCP Server 进程。

禁止记录：

- API key
- auth header
- 完整敏感环境变量

---

## 20. Codex STDIO MCP 配置目标

项目实现完成后，应能由 Codex 使用类似配置启动：

```toml
[mcp_servers.codex-claude-agent]
command = "uv"
args = [
  "--directory",
  "D:\\path\\to\\trusted-project",
  "run",
  "python",
  "-m",
  "codex_claude_agent_mcp"
]
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
# 可选长期信任根；为空时由每次调用的 project_root 授权
ALLOWED_PROJECT_ROOTS = 'D:\projects'
```

或者安装成可执行入口后：

```toml
[mcp_servers.codex-claude-agent]
command = "codex-claude-agent-mcp"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 7200

[mcp_servers.codex-claude-agent.env]
# 可选长期信任根；为空时由每次调用的 project_root 授权
ALLOWED_PROJECT_ROOTS = 'D:\projects'
```

最终字段名必须以实现当时的 Codex 官方文档为准。

---

## 21. Server 启动要求

模块入口：

```bash
python -m codex_claude_agent_mcp
```

启动后：

- 不打印普通 stdout 文本。
- 初始化 MCP STDIO transport。
- 初始化 SQLite。
- 初始化 ClaudeRunner。
- 等待 MCP 请求。
- MCP Client 关闭连接后正常退出。

---

## 22. Tool Result 上下文控制

不要把整个 Claude transcript 返回给 Codex。

Execution 返回：

```text
status
session_id
summary
files_changed
validation
blocked_reason
```

Review 返回：

```text
PASS/FAIL
summary
unmet_criteria
evidence
review_session_id
```

完整 Claude session 留在 Claude/session storage 中。

---

## 23. 测试要求

### 23.1 单元测试

覆盖：

- schema validation
- review=true + no acceptance -> error
- review=false + no acceptance -> valid
- duplicate job_id
- per-process semaphore
- state transitions
- invalid cwd
- session resume lookup
- 任务级 `project_root`：`ALLOWED_PROJECT_ROOTS` 之外可授权、`cwd` 越界拒绝、
  省略时回落长期根、`continue_task` 继承、`review_task` 继承或完全匹配、
  `run_job` / `run_jobs` 透传、SQLite 旧库迁移

### 23.2 STDIO 协议测试

必须验证：

1. stdout 没有普通日志。
2. stderr 可正常输出日志。
3. 一个 Server 进程连续处理多个 Tool Call。
4. Tool Call 之间不需要重启 MCP。
5. Claude 普通输出不会污染 stdout。

### 23.3 Fake Claude Runner

抽象：

```text
ClaudeRunner
```

提供：

```text
FakeClaudeRunner
```

模拟：

- success
- blocked
- failure
- timeout
- review pass
- review fail

### 23.4 多进程测试

启动两份独立 MCP Server 进程，验证：

- 可以同时存在。
- SQLite 不损坏。
- job_id 冲突按设计处理。
- 一个进程崩溃不破坏另一个进程。
- 各自 stdout 只属于各自 MCP 连接。

### 23.5 真实集成测试

至少：

1. `execute_task` 完成真实简单代码任务。
2. 获取 execution_session_id。
3. `continue_task` resume 原 session。
4. `review_task` 创建不同 session_id。
5. `run_job(review=true)` 执行后 review。
6. `run_jobs` 3 个独立任务并发。
7. MCP 空闲时无 Claude active session。
8. 同一 MCP 进程连续执行多个调用不重启。

---

## 24. Codex 实施步骤

### Step 1
建立 Python/uv 项目。

### Step 2
接入官方 MCP Python SDK 的 STDIO Server。

### Step 3
先实现最小 `ping`/测试 Tool，验证：
- Codex 能启动 MCP
- stdin/stdout 正常
- stderr 日志不污染协议

### Step 4
实现 schema + config。

### Step 5
实现 SQLite store。

### Step 6
实现 `ClaudeRunner` + Fake runner。

### Step 7
接 Claude Agent SDK。

### Step 8
实现 `execute_task`。

### Step 9
实现 `review_task`。

### Step 10
实现 `continue_task`。

### Step 11
实现 `run_job`。

### Step 12
实现 `run_jobs` + per-process concurrency。

### Step 13
执行 multi-process store 测试。

### Step 14
给出 Windows Codex STDIO 配置与启动说明。

---

## 25. v1 完成定义

- [ ] 使用 STDIO MCP，不启动 HTTP Server。
- [ ] Codex 可以通过 command/args 启动 Server。
- [ ] stdout 只承载 MCP 协议。
- [ ] 日志全部 stderr。
- [ ] 一个已启动 MCP Server 能处理多次 Tool Call。
- [ ] 不为每次 Tool Call 重启 MCP Server。
- [ ] 能同时运行多个独立 MCP Server 进程而不破坏状态。
- [ ] `execute_task` 可以调用 Claude Agent SDK。
- [ ] 保存 execution_session_id。
- [ ] `continue_task` 能 resume 原 execution session。
- [ ] `review_task` 使用全新 session。
- [ ] reviewer 默认不修改项目。
- [ ] `review=true` 无 acceptance 明确报错。
- [ ] MCP 不自行生成 acceptance。
- [ ] MCP 不自行拆任务。
- [ ] `run_jobs` 支持单实例内并发。
- [ ] SQLite 支持多进程访问场景。
- [ ] 核心单元测试、STDIO 测试、多进程测试通过。
- [ ] README 提供 Windows Codex STDIO MCP 配置。

---

## 26. 官方实现参考

- Codex MCP  
  https://developers.openai.com/codex/mcp

- MCP Specification / STDIO Transport  
  https://modelcontextprotocol.io/specification/

- MCP Python SDK  
  https://py.sdk.modelcontextprotocol.io/

- Claude Agent SDK Overview  
  https://code.claude.com/docs/en/agent-sdk/overview

- Claude Agent SDK Quickstart  
  https://code.claude.com/docs/en/agent-sdk/quickstart

- Claude Agent SDK Python Reference  
  https://code.claude.com/docs/en/agent-sdk/python

- Claude Agent SDK Sessions  
  https://code.claude.com/docs/en/agent-sdk/sessions

- Claude Agent SDK Permissions  
  https://code.claude.com/docs/en/agent-sdk/permissions

- Claude Agent SDK Hooks  
  https://code.claude.com/docs/en/agent-sdk/hooks
