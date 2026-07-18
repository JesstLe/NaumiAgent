# HAR-09 Feedback Candidate 与受控提升

## 目标

把重复失败和用户纠正聚合为可审查 Proposal，而不是直接修改 Prompt、规则或代码。

## 子模块

- HAR-09.1 Fingerprint：failure class、module、tool、normalized context 的隐私安全指纹。
- HAR-09.2 Aggregator：次数、最近发生、影响范围、趋势和代表 Evidence。
- HAR-09.3 Candidate policy：最小次数、严重度、冷却期、排除一次性噪声。
- HAR-09.4 Proposal generator：knowledge/profile/prompt/tool/test/code 六类建议。
- HAR-09.5 Review queue：approve/reject/defer/merge，必须有人类或治理策略决定。
- HAR-09.6 Outcome tracking：Proposal 实施后用 HAR-08 比较，记录改善或回退。

## 安全与隐私

- fingerprint 不包含原始用户文本、secret、路径绝对前缀或 stdout。
- 用户拒绝/取消不自动视为 Agent 缺陷。
- 同一根因跨模型重复时合并，但保留 provider/model 维度用于分析。
- Proposal 只写候选表和 Workbench review，不自动触碰仓库。

## 验收标准

- 相同规范化失败聚合，语义不同失败不碰撞；测试固定 collision fixtures。
- 少于阈值不生成候选，高严重度安全问题可走单次升级规则。
- reject 后冷却期内不重复骚扰；新证据显著变化可重新开启。
- Proposal 实施后必须关联 Eval before/after；无改善则不能标为 promoted。
- A3：注入重复失败、用户纠正和噪声，最终队列数量与规则一致。

## 已具备的跨模块前置

- EVO-01.2a 已提供稳定、脱敏且固定不可执行的 Candidate Draft 契约，可作为未来 HAR-09
  Proposal/Review Queue 的输入边界。
- EVO-01.3a 已提供用户级 versioned Candidate Store、不可变 Evidence、幂等并发 merge 和
  digest audit chain；HAR-09 必须复用该 Store，不再创建第二套候选表。
- HAR-09.1a 已实现可信 Feedback Intake：直接 `/feedback` 与 Agent interpretation 使用不可混淆
  source kind；Agent Tool 必须绑定 runtime 签发的 durable Chat Run 信封；偏好、取消、赞扬不
  形成缺陷，摘要原文不落库。详见 `HAR-09-1a-trusted-feedback-intake.md`。
- HAR-09.2a 已实现 Candidate Aggregation View v1：以 Candidate 最后观测为稳定 anchor，计算
  24h/7d/30d、前一 7d、趋势、source/provider/model/platform 分布及代表 Evidence；纯函数不写
  Store。详见 `HAR-09-2a-candidate-aggregation-view.md`。
- HAR-09.3a 已实现 Candidate Policy v1：单条机械证据或至少两条直接用户反馈可进入人工审阅；
  单次反馈、Agent-only 信号保持证据不足，authority-bearing scope 机械阻断。策略只输出版本化
  Assessment，不创建 Proposal、不写 Review Queue、不授予实验资格。详见
  `../self-evolution/EVO-01-4a-candidate-eligibility-policy.md`。
- HAR-09.4a 已实现确定性 Proposal Preview v1：仅对 `review_ready` Candidate 生成
  knowledge/profile/prompt/tool/test/code 六类建议、稳定 source snapshot、相对目标文件和机械验证计划；
  `/evolution detail` 与 Agent Tool 使用同一 Preview。Preview 固定不可执行、不写 Workbench Queue，
  详见 `HAR-09-4a-proposal-preview.md`。
- HAR-09.5a 已实现显式、幂等的 Workbench Queue Foundation：保存 Candidate revision/digest 与
  Preview provenance，校验 mission/issue 绑定，8 路并发只产生一条 Proposal 和一条审计事件；slash、
  Agent Tool 与 New UI 复用同一 Adapter。详见 `HAR-09-5a-workbench-queue-foundation.md`。
- HAR-09.5b1 已实现版本化 approve/reject/defer/merge 状态机、CAS 并发决策、30 天 reject 冷却、
  有界 defer、同 Candidate 高 revision merge，以及“风险升级或至少 max(2, 50%) 新证据”提前重开；
  Evolution 再入队已强制执行该策略。详见 `HAR-09-5b1-governance-cooldown.md`。
- HAR-09.5b2a 已实现 Eligibility v2 治理上下文接线：Review Service 批量读取最新 Proposal，
  typed New UI 和 Markdown 同步显示 cooldown 原因、状态、revision 与截止时间；活跃冷却不再显示
  `review_ready` 或生成 Preview。详见 `HAR-09-5b2a-eligibility-governance-context.md`。
- HAR-09.5b2b/UI-10.6a 已实现 Workbench Proposal 决策交互：New UI 与 TUI 在同一 Reviews 列表中
  展示 open Proposal，normal 模式确认、bypass 无二次确认，并复用既有治理状态机和审计。当前只开放
  approve/reject/cancel，批准不执行代码、不授予实验资格。详见
  `../cli-ui/UI-10-6a-proposal-actions.md`。
- HAR-09 仍未实现 defer/merge 交互、approved 到 EVO-02 的显式转换和 outcome tracking，因此整体
  继续保持 partial。
