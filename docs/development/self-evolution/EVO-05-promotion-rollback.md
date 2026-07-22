# EVO-05 提升、回滚与发布治理

## 目标

将通过实验的 patch 以可审查、可签名、可分阶段回滚的方式进入产品；默认创建 Proposal/PR，
不自动合并或推送 main。

## 子模块

- [EVO-05.1a Promotion Package Input](EVO-05-1a-promotion-package-input-contract.md)：已交付；只从 active
  accepted Reflection 冻结 patch、baseline、receipts、risk、migration 与 rollback plan，不授予执行权。
- [EVO-05.1b Promotion Package](EVO-05-1b-promotion-package-contract.md)：已交付；消费仍 eligible 的 Input，
  绑定 exact local target branch、签名域和审批事实，但不审批或执行 Git。
- EVO-05.2 Approval policy：按风险要求 user/reviewer/signature；protected scope 永远人工。
- EVO-05.3 Rebase/revalidate：目标 main 变化后重放 patch 并重新验证，旧结果失效。
- EVO-05.4 Staged rollout：local canary、opt-in channel、percentage、stable。
- EVO-05.5 Runtime monitor：错误、性能、completion、用户撤回信号与阈值。
- EVO-05.6 Rollback：binary/config/schema/patch 的兼容回滚和数据保护。
- EVO-05.7 Outcome record：promoted/rolled_back/superseded 与长期指标。

## 验收标准

- 未审批 package 无法进入主分支/稳定 channel，即使 bypass。
- rebase 后任何生产文件变化都使旧 Eval receipt stale。
- canary 超 guardrail 自动停止扩大并建议回滚，不删除诊断证据。
- rollback 在断电/进程崩溃中保持至少一个可启动版本。
- migration 不可逆时 promotion 必须阻断或提供前向恢复方案。
- 真实 patch 从 Proposal、审批、canary、监控到回滚完整演练。

## 当前边界

当前完成 EVO-05.1a/1b。尚无 approval、签名收集、rebase/revalidate、rollout、monitor、rollback executor 或 Outcome
authority；因此任何界面和回执都不得宣称已 promotion、merge、push 或发布。
