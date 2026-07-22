# EVO-05 提升、回滚与发布治理

## 目标

将通过实验的 patch 以可审查、可签名、可分阶段回滚的方式进入产品；默认创建 Proposal/PR，
不自动合并或推送 main。

## 子模块

- [EVO-05.1a Promotion Package Input](EVO-05-1a-promotion-package-input-contract.md)：已交付；只从 active
  accepted Reflection 冻结 patch、baseline、receipts、risk、migration 与 rollback plan，不授予执行权。
- [EVO-05.1b Promotion Package](EVO-05-1b-promotion-package-contract.md)：已交付；消费仍 eligible 的 Input，
  绑定 exact local target branch、签名域和审批事实，但不审批或执行 Git。
- [EVO-05.2a Approval Requirement Policy](EVO-05-2a-approval-requirement-policy.md)：已交付；按风险、target、
  protected scope 与 migration 冻结角色、签名门、技术门和有效期，不创建交互或作出决定。
- [EVO-05.2b Approval Request Authority](EVO-05-2b-approval-request-authority.md)：已交付；把单个 required
  role 映射为 HAR-10.6 fenced interaction，并冻结 approval/rejection/request-changes Receipt 与独立签名入口。
  未验证的专业角色不能计入 quorum。
- [EVO-05.2c1 Approval Principal & Public-Key Authority](EVO-05-2c1-approval-principal-authority.md)：已交付；
  通过 HAR 人工确认管理 trusted principal、role binding、Ed25519 公钥注册、轮换与撤销，不接收私钥。
- [EVO-05.2c2 Approval Signature Receipt Authority](EVO-05-2c2-approval-signature-receipt-authority.md)：已交付；
  通过 nonce/expiry Challenge 验证 current active key 对 exact domain payload 的真实 Ed25519 签名，并动态阻断
  撤销、旧 generation、过期与跨域重放。
- EVO-05.2d Approval Decision Aggregation：重读全部角色、身份、签名与技术门，形成非 Git 执行型决定。
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

当前完成 EVO-05.1a/1b、EVO-05.2a/2b 与 EVO-05.2c1/2c2。已有经用户确认的 Principal/role/public-key
authority，以及真实外部 Ed25519 Challenge/Signature Receipt；但尚无最终 approval decision、
rebase/revalidate、rollout、monitor、rollback executor 或 Outcome authority；因此任何界面和回执都不得宣称已
promotion、merge、push 或发布。
