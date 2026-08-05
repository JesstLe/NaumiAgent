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
- [EVO-05.2a1 Professional Role Signature Policy v2](EVO-05-2a1-professional-role-signature-policy-v2.md)：
  已交付；所有非 user 专业角色必须用 current Principal key 签署 exact Response，legacy v1 保持只读兼容。
- [EVO-05.2b Approval Request Authority](EVO-05-2b-approval-request-authority.md)：已交付；把单个 required
  role 映射为 HAR-10.6 fenced interaction，并冻结 approval/rejection/request-changes Receipt 与独立签名入口。
  未验证的专业角色不能计入 quorum。
- [EVO-05.2c1 Approval Principal & Public-Key Authority](EVO-05-2c1-approval-principal-authority.md)：已交付；
  通过 HAR 人工确认管理 trusted principal、role binding、Ed25519 公钥注册、轮换与撤销，不接收私钥。
- [EVO-05.2c2 Approval Signature Receipt Authority](EVO-05-2c2-approval-signature-receipt-authority.md)：已交付；
  通过 nonce/expiry Challenge 验证 current active key 对 exact domain payload 的真实 Ed25519 签名，并动态阻断
  撤销、旧 generation、过期与跨域重放。
- [EVO-05.2d Approval Decision Aggregation](EVO-05-2d-approval-decision-aggregation.md)：已交付；重读全部
  角色、身份、签名与技术门，形成 append-only、可动态失效、非 Git 执行型决定。
- [EVO-05.3a Revalidation Request Authority](EVO-05-3a-revalidation-request-authority.md)：已交付；只把 current
  approved Decision 与 exact Package 冻结为可动态失效、非执行型隔离请求。
- EVO-05.3b1 Exact-target replay：已在 disposable detached worktree 真实重放 Candidate bytes 并签发 durable Receipt；
- EVO-05.3b2a Target-advance authority：已证明 target-only stale 与线性前进关系，形成合法隔离 rebase 输入；
- [EVO-05.3b2b Fenced three-way rebase](EVO-05-3b2b-fenced-three-way-rebase.md)：已在隔离 current-target
  worktree 真实三方重放，持久化冲突/失败、claim、epoch fencing 与恢复；
- [EVO-05.3c Harness revalidation evidence](EVO-05-3c-harness-revalidation-evidence.md)：已重新构造 exact/rebase
  overlay，经 Harness Sandbox 与 ARC-04 Worker 执行匹配检查并签发新证据；
- [EVO-05.3d Revalidation Outcome](EVO-05-3d-revalidation-outcome.md)：已交付；原子失效旧 evidence，形成可动态
  stale 的 rollout 候选结论，并强制 Final Evaluation、专业签名与 Approval 重新签发/聚合。
- [EVO-05.3e Fresh Evaluation Plan](EVO-05-3e-fresh-evaluation-plan.md)：已交付；从 current validated Outcome
  冻结完整 Interventional + 跨平台 Adversarial 重评矩阵，禁止用 Harness check 替代 Final Evaluation。
- [EVO-05.3f1 Immutable Evaluation Source](EVO-05-3f1-immutable-evaluation-source.md)：已交付；把 rebase 后 exact
  target + overlay 固化为 content-addressed blobs，评测不再依赖旧 Candidate worktree 存活。
- [EVO-05.3f2a Revalidation Validation Plan](EVO-05-3f2a-revalidation-validation-plan.md)：已交付；将 current target
  绑定为相同 RED/GREEN baseline，将 immutable overlay 仅绑定 GREEN，并冻结原 seed/预算/指标/样本与 current checks。
- [EVO-05.3f2b1 Runtime Source Pair](EVO-05-3f2b1-runtime-source-pair.md)：已交付；为两类 Harness Eval 统一提供
  current-target RED 与 immutable-overlay GREEN，并在执行前后动态复验 source authority。
- [EVO-05.3f2b2a Fresh Runtime Contract](EVO-05-3f2b2a-fresh-runtime-contract.md)：已交付；重新绑定 metric runner、
  timeout、完整预算与 adversarial probe coverage，任何缺口均阻断真实 sample execution。
- [EVO-05.3f2b2b1 Fresh Interventional Sample](EVO-05-3f2b2b1-fresh-interventional-sample.md)：已交付；真实执行并
  持久化一对 RED/GREEN H5a sample，支持中断恢复且不授予 cohort/comparison/promotion authority。
- [EVO-05.3f2b2b2 Fresh Interventional Cohort](EVO-05-3f2b2b2-fresh-interventional-cohort.md)：已交付；在共享
  cohort Run Grant 下执行连续 RED/GREEN 样本、验证可恢复前缀并冻结原始 metric/check evidence。
- [EVO-05.3f2b2b3 Fresh Interventional Comparison](EVO-05-3f2b2b3-fresh-interventional-comparison.md)：已交付；
  从原始 H5a 重算 cohort summary，并形成 HAR-08 原生 H5b2/H5c comparison authority。
- [EVO-05.3f2c1 Fresh Adversarial Sample](EVO-05-3f2c1-fresh-adversarial-sample.md)：已交付；在指定真实平台运行
  current-target RED 与 immutable-overlay GREEN probe pair，但尚未形成平台 cohort/matrix。
- [EVO-05.3f2c2 Fresh Adversarial Cohort](EVO-05-3f2c2-fresh-adversarial-cohort.md)：已交付；形成单一真实平台的
  连续 probe cohort、共享 Grant 和可恢复前缀，但尚未完成 required-platform matrix。
- [EVO-05.3f2c3a Fresh Adversarial Matrix Status](EVO-05-3f2c3a-fresh-adversarial-matrix-status.md)：已交付；动态区分
  completed/runnable/pending platform lane，只有全部 cohort 齐全才持久化完成矩阵，尚不授予远端调度或比较权威。
- [EVO-05.3f2c4 Fresh Adversarial Comparison](EVO-05-3f2c4-fresh-adversarial-comparison.md)：已交付；完整 matrix 后
  对每个平台从原始 H5a 重算 probe/identity/summary，并持久化 HAR-08 原生 H5b2/H5c。
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

当前完成 EVO-05.1a/1b、EVO-05.2a-2d 与 EVO-05.3a-3f2c4。已有真实签名审批、target 前进后的隔离 replay/rebase、
Harness revalidation、旧证据失效、Fresh Evaluation matrix、immutable GREEN source 与 current-target Validation Plan；
Fresh Interventional cohort 与 paired comparison 已完成，但 Adversarial lanes 尚未完成，也没有新 Final Evaluation、
reapproval、rollout、monitor、rollback executor 或最终发布 Outcome authority。任何界面和回执都不得把 cohort
宣称为 promotion、merge、push 或发布。
