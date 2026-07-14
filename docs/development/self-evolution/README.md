# 自进化闭环模块册

## 当前事实

- `self_review` 已有真实静态扫描 + 可选 LLM 综合，不是纯 Prompt 套壳。
- `self_modify` 已有路径保护、备份、ruff/compile/pytest 验证和回滚机制。
- `self_evolve` 已有变更评估与 apply/reject/rollback 决策路径。
- Pursuit 已有持久目标、criteria、checkpoint、worktree、等待和停止决定。

这些能力仍不足以自动提升生产版本：当前缺统一候选 Store、标准 Eval before/after、隔离变异
治理、防奖励投机、推广审批、分阶段发布和长期效果跟踪。

## 闭环

`Evidence → Candidate → Isolated Mutation → Validation/Eval → Reflection Decision →
Promotion/Rollback → Feedback`。

每一箭头都有持久化输入/输出和 Harness receipt；任何一门失败都停止，不允许模型用自然语言
宣称绕过。

## 分阶段权限

- Phase E：只读自审，可默认运行。
- Phase F：只在隔离 worktree 变异，需要明确 scope/budget。
- Phase G：Eval 与反思决定，默认不合并。
- Phase H：能力扩展与推广，必须人工或签名治理策略批准。
