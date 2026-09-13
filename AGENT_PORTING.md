# 点将台：迁移到其他 Agent 组合

本项目当前组合是 **Codex（编排者）→ MCP → Claude Code（执行者/Reviewer）**。
如果使用其他 Agent，不要只替换名称；先判断需要迁移哪一层。

## 通用角色模型

```text
主脑 / Commander Agent
  ├─ 做需求、架构、边界和最终验收
  └─ 产生 task + scope + acceptance + cwd
                 │
                 ▼
      点将台 MCP（确定性控制层）
        ├─ Worker：调查、实现、验证
        └─ Reviewer：全新只读会话、独立审查
```

Worker 和 Reviewer 可以使用同一 Agent 的不同会话，也可以使用两个不同 Agent；
必须分别配置能力与权限，不能把“换了模型”当成“独立会话”的替代。

## 三种情况

| 目标组合 | 需要修改 |
| --- | --- |
| 其他编排 Agent → Claude Code | MCP 通常不用改；修改客户端 STDIO 配置，并把 Codex Skill 改写成该 Agent 能读取的规则。 |
| Codex → 其他执行 Agent | 保留 MCP 工作流，替换 `claude_runner.py` 的 SDK/CLI 适配层、鉴权、模型配置与错误映射。 |
| 其他编排 Agent → 其他执行 Agent | 同时完成以上两项；不要让 MCP 承担语义规划。 |

## 迁移前确认执行端能力

新的执行端至少应支持：

1. 指定 `cwd` 启动任务；
2. 返回稳定 session id，并能恢复原执行会话；
3. 为 Review 创建全新会话；
4. 分别限制执行与只读 Review 的工具权限；
5. 返回可校验的结构化结果；
6. 支持超时、取消和错误分类。

缺少某项时必须明确降级行为，不能伪造 session、Review 或成功状态。

## 必须保留的 MCP 规则

- MCP 只做确定性调度、状态和权限校验，不拆任务、不创造验收标准。
- `job_id` 永久绑定 task、acceptance、`cwd`、`project_root` 和 session。
- `project_root` 只授权当前 job；`continue_task` 不得切换或扩大目录。
- Review 使用全新只读会话，不能恢复执行会话，也不能修改项目。
- 只有执行端确认过的 execution session 才能 resume。
- 超时、取消或协议中断保存为对应 stage `INCOMPLETE`，不得冒充完成。
- `review=false` 不得产生隐藏 Review 或额外模型调用。
- stdout 只输出 MCP 协议；日志只写 stderr。
- 一个 MCP 进程处理多次 Tool Call；并发和 SQLite 原子状态规则保持不变。

## 建议迁移步骤

1. Fork 或复制到独立目录，使用目标编排者前缀命名；不要覆盖本项目。
2. 保持现有 MCP Tool schema，先实现新的 Runner 适配器。
3. 用 Fake Runner 跑通全部现有测试，再增加新 SDK 的 session、权限、取消和错误测试。
4. 改写 README、安装配置、包名及环境变量；清除旧平台专用说明。
5. 为新的编排 Agent 单独建立轻量规则文件/Skill，不要原样安装 Codex Skill。
6. 用一个临时项目完成 execute → status → continue → fresh review 的真实链路验证。
7. 扫描密钥、账号、模型中转地址和本机绝对路径后，再建立新 Git 历史或发布。

## 可直接交给目标 Agent 的迁移指令

```text
目标：把“点将台”从 Codex → Claude Code 迁移为：
- 主脑/编排端：{COMMANDER_AGENT}
- 执行端：{WORKER_AGENT}
- Reviewer：{REVIEWER_AGENT；默认 WORKER_AGENT 的全新只读会话}
- 原项目：{SOURCE_DIR}
- 新项目：{TARGET_DIR}

在修改前先完成能力审计，并用简短表格报告：
1. 编排端是否支持本地 STDIO MCP、结构化 Tool Result 和规则文件/Skill；
2. 执行端是否支持指定 cwd、启动并返回稳定 session、恢复原 session、工具权限控制、
   结构化输出、超时和取消；
3. Reviewer 是否能保证全新会话和只读工具；
4. 本次属于“只换编排端”“只换执行端”还是“两端都换”。

发现能力缺口时不要猜接口、伪造 session、伪造 Review 或降低目录安全。先查目标 Agent
的实际 SDK/CLI；仍缺失时列出最小降级及其影响，只把真正需要用户决定的事项上报。

实施要求：
- 在 TARGET_DIR 创建独立迁移项目，不覆盖 SOURCE_DIR，不修改 ZCode 或其他同类项目；
- 不做全仓机械改名。先保持 MCP Tool schema 和工作流核心，再把平台差异隔离到 Runner、
  客户端配置、提示词/工具权限、鉴权、模型配置与错误映射；
- MCP 只做确定性调度和校验，不拆任务、不选择模型、不创造 acceptance；
- 永久保留 job/task/acceptance/cwd/project_root/session 绑定、Job 临时授权、continue 不扩权、
  fresh read-only review、confirmed session 才可恢复、INCOMPLETE 语义、SQLite 原子状态、
  有界并发以及 stdout 仅承载 MCP 协议；
- 编排规则保持轻量：主脑保留未决产品/架构/安全决策与最终验收；范围明确且可独立验证的
  实现、排错、重构、测试、算法、迁移和数据/建模工作优先派发；只有真实决策点才回传；
- 执行者负责必要验证；Reviewer 独立审查代码、逻辑和证据，不默认重复全部测试；
- 不写入密钥、账号、真实模型中转地址或本机私有绝对路径。

验证要求：
1. 先让 Fake Runner 的现有回归全部通过，再增加目标 SDK/CLI 的适配测试；
2. 覆盖启动/确认/resume session、fresh review、只读限制、cwd/project_root 越权、job 串线、
   超时取消、协议错误、并发与多进程状态；
3. 在临时项目跑一次真实 execute → get_job_status → continue → review 链路；
4. 同步 README、安装说明、环境变量、包名和新的编排 Skill；执行隐私扫描；
5. 若目标端没有某项能力，测试必须证明降级会明确报错，而不是静默成功。

工作方式：优先自行检查仓库和目标工具，不把大段源码复制回主会话；保持改动最小、可审查、
可回滚。完成后只报告：迁移类型、架构差异、能力降级、安装方法、测试/实测结果、隐私扫描、
是否需要重启，以及仍需用户决定的事项。
```

## 许可

迁移版本仍受仓库 `LICENSE` 约束：仅限非商业使用；复制、修改或形成 Covered Work 时
须保留署名，并按同一许可公开 Corresponding Source。商用需另行获得书面授权。
