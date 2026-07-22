# 自进化闭环模块册

## 当前事实

- `self_review` 已有结构化 AST 静态扫描、脱敏 Evolution Evidence 与可选 LLM 综合，不是纯 Prompt 套壳。
- `self_modify` 已有路径保护、备份、ruff/compile/pytest 验证和回滚机制。
- `self_evolve` 已有变更评估与 apply/reject/rollback 决策路径。
- Pursuit 已有持久目标、criteria、checkpoint、worktree、等待和停止决定。

Candidate Draft 契约和用户级 Candidate Store Core 已经存在，但这些能力仍不足以自动提升
生产版本：当前缺完整来源 adapter、跨 provider/model/platform 的聚合策略、eligibility、
标准 Eval before/after、隔离变异治理、防奖励投机、推广审批、分阶段发布和长期效果跟踪。

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

## Agent Tool 权限治理

[EVO-GOV-01](EVO-GOV-01-agent-tool-permission-matrix.md) 已为 EVO-03.7 与 EVO-04.1-4.4 的七个 durable
派生 Tool 建立精确权限规则：中风险、normal 无逐次确认、strict 可用、lockdown 阻断、bypass 全权限，
并按 Evaluation/Decision family 设置会话上限。以后新增非只读 Evolution Tool 必须与权限规则和注册表门
同一切片交付。

EVO-04.4a 已交付真实字节驱动的 Counterfactual Evidence：从 completed Independent Review 重读完整
authority 链，对受管 worktree 的 baseline/candidate/diff 与 Mutation Receipt 做逐文件复核，并检查更小
scope、删测试、metric/threshold、skip/mock 与评测泄漏。它不调用模型、不保存源码，也不形成最终 decision。
