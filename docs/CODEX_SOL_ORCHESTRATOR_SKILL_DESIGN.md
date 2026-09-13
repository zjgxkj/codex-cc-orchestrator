# 点将台：Codex / CC 编排规则

实际执行以 [Skill](../skills/codex-sol-claude-orchestrator/SKILL.md) 为准；困难算法/建模、视觉、关键瓶颈或修复无效时才读 [条件参考](../skills/codex-sol-claude-orchestrator/references/capability-boundaries.md)。

## 职责

- **Skill** 只做一次早期路由；**MCP** 只执行、保存/恢复 session、按调用创建独立 Review，不做语义拆分或自动路由。
- **CC-1** 自行调查仓库、实现并运行与范围/风险相称的验证。
- **CC-2** 在全新会话中只用 Read/Grep/Glob 审核代码与证据，不修改、不运行测试。
- **Codex** 决定用户目标和重大契约，解决最小关键瓶颈或无进展的反复修复，并做证据驱动的最终验收；不默认跑常规测试或重复完整 Review。

CC 是 Claude Code 框架。模型映射、provider 和工具是不同事实，不按名称或一次网络失败判断能力。

## 路由

纯讨论/规划、极小机械修改、或 CC 缺必要输入/工具时留在 Codex。其他任务只要能形成有意义、可访问、边界清楚、可验收且值得调用的工作包，就默认交 CC；答案/根因/文件未知和算法困难不构成例外。

Codex 不为派单先通读仓库、找完根因或设计完整实现。它只先解决必须明确的产品、公共接口、架构、权限/安全、数据/兼容性、不可逆迁移或建模假设；局部可逆选择由 CC 决定。关键难点只收回最小决定性部分，稳定周边继续委派。

任务包只含 `job_id`、`cwd`、目标、范围、关键约束、CC 无法自行获得的事实/素材和可检查的 acceptance。并行仅用于接口稳定、写入不重叠的独立包。

## 验证、Review 与停止

- Bug 触发或原因不清时，CC-1 先复现、修改后用同一条件复验；可靠失败测试或明确错误证据已经算复现。普通改动跑针对性测试，高风险再扩大；不为流程强跑全套。
- 实质逻辑变更默认新 CC-2 Review；只读调查和真正机械、行为不变的编辑可跳过。执行验证不是第三层。
- CC-2 主要找逻辑、边界、错误假设、回归/契约/安全风险和证据不匹配。缺证据或有普通缺陷时，恢复 CC-1 补最小验证/修复，再开新 CC-2；Codex 不夹在中间承担常规测试。
- 只要新证据和因果认识在推进，循环可继续；同一核心问题不变、掩盖症状、持续回归、证据停滞或出现重大决策时，Codex 接手最小关键部分。无固定重试次数。
- Codex 最终根据用户目标、变更、CC-1 验证、CC-2 verdict/evidence 和关键约束定点验收；仅在证据冲突/缺失、未解 FAIL、高风险不变量、反复修复或目标可能误解时深入验证。

## MCP 约束

`execute_task` 不自动 Review；`run_job(review=true)` 执行后开全新 Review；普通返修用 `continue_task` 恢复 CC-1。相同 `job_id` 必须保持 task/acceptance/cwd/`project_root` 不变，变更目标或项目根就换 ID。派单时必须显式传 `project_root`：来自 Codex 自身 workspace 上下文（或该 job 明确选定的项目根），绝不能是 MCP 进程 cwd、猜测路径或整盘根；它只授权该 job，`continue_task` 无此入参并继承已存值。超时后先查真实状态，不能盲目重做。MCP 仍保留 cwd、session、结构化输出和权限边界；Reviewer 无 Bash/写权限，图片附件不会从 Codex 会话自动转发。

Skill 可自动匹配，也可显式写 `$codex-sol-claude-orchestrator`；自动选择不是强制钩子。源文件与 `~/.codex/skills/codex-sol-claude-orchestrator` 应保持一致，其他客户端项目和 Skill 不参与本流程。
