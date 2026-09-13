# 迁移到其他 Agent 组合

本项目当前组合是 **Codex（编排者）→ MCP → Claude Code（执行者/Reviewer）**。
如果使用其他 Agent，不要只替换名称；先判断需要迁移哪一层。

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
把当前项目从“Codex → Claude Code”迁移为“{编排 Agent} → {执行 Agent}”。

先判断只需更换编排端、执行端，还是两端都更换。不要机械替换名称。
优先保持现有 MCP Tool schema、job/session/cwd/project_root 绑定、临时目录授权、
只读独立 Review、INCOMPLETE 状态、SQLite 原子状态和 STDIO 纪律。

若更换执行端，请用其官方 SDK/CLI 实现 Runner 适配层，并验证启动 session、恢复执行
session、新建 Review session、工具权限、结构化输出、超时取消和错误分类。缺失能力必须
明确报告并设计最小降级，禁止伪造成功。

若更换编排端，请提供该客户端的 STDIO MCP 安装配置，并把编排 Skill 改成它实际支持的
规则格式。MCP 不负责智能拆解；编排者保留决策和最终验收，执行者接收明确 task、scope、
acceptance 和 cwd。

在独立目录完成迁移，不修改原项目；同步文档和测试，跑完整回归与一次真实链路。最后报告
架构差异、降级项、安装方法、测试结果和是否需要重启客户端。
```

## 许可

迁移版本仍受仓库 `LICENSE` 约束：仅限非商业使用；复制、修改或形成 Covered Work 时
须保留署名，并按同一许可公开 Corresponding Source。商用需另行获得书面授权。
