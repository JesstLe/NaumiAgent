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
- UI-10.6b1 已补齐既有 `ProposalAction.DEFER` 的 New UI/TUI 入口：必填原因、1/7/30 天预设、
  authority clock、normal 一次确认与 bypass 无二次确认均复用 HAR-09.5b1 Service/CAS/cooldown/audit；
  它不新增 Harness authority，详见 `../cli-ui/UI-10-6b1-proposal-defer.md`。
- UI-10.6b2 已补齐既有 `ProposalAction.MERGE` 的 New UI/TUI 入口：共享 Service 为 open Proposal
  投影同 Candidate 的较新 open revision，前端只选择目标，最终仍由 HAR-09.5b1 规则重验并 CAS；
  normal 一次明确确认，bypass 无二次确认，详见 `../cli-ui/UI-10-6b2-proposal-merge.md`。
- HAR-09.5c/UI-10.6c 已实现 approved Evolution Proposal 到 durable Experiment Contract Authority 的
  显式转换：同一 Proposal 并发/重复签发单飞，normal 确认、bypass 直接执行，Agent Tool 与 New UI/TUI
  复用同一 issuer；回执固定 `execution_ready=false`。详见
  `HAR-09-5c-explicit-experiment-contract-issuance.md`。
- EVO-04.5a 已补齐 promotion 前的 Reward-hacking Evidence，EVO-04.6a 又形成不可变四态 Decision State；
  EVO-04.6b 已把 escalation 用户答案形成不可变 Resolution。`accepted_experiment` 仍只设置
  `promotion_review_ready`，不执行 promotion。EVO-04.7a 已将 Decision/Resolution 投影为非注入、可撤销的
  结构化 Reflection Memory。EVO-05.1a/1b 已进一步冻结不可执行 Promotion Package Input 与 exact-target
  review Package；EVO-05.2a 又冻结了不可执行 Approval Requirement。但 HAR-09.6 的 promoted 路径仍需显式
  promotion executor 和完整 Outcome authority，不能把 Decision、Resolution、Reflection、Package Input、
  Package 或 Approval Requirement 直接记为 promoted outcome。
- EVO-05.6b2a 已提供第一个真实 `rollback_executed=true` 的 authority-bound slot Receipt，并明确
  `outcome_recorded=false`；这关闭了无数据迁移回滚的执行事实前置，但 Receipt 本身仍不能直接标记为 Proposal outcome。
- EVO-05.6b2a1 已把该事实接到受权限治理的 Agent Tool 与共享 Slash Router；normal 只有一次高风险确认，bypass
  不确认，New UI/TUI 回显同一 durable Receipt。该产品入口仍不改变 `outcome_recorded=false`。
- EVO-05.7a 已新增独立 Proposal-bound `rolled_back` Outcome：通过 Fresh/Prior Promotion Input 和原始 Experiment
  Contract Authority 反向绑定 Workbench Proposal，且篡改动态撤权。它尚无 HAR-08 before/after 或长期指标，因此
  HAR-09.6 仍为 partial，Workbench 不得据此显示 promoted 或触发 policy learning。
- HAR-09.6a 已把该 Outcome 以只读方式投影到 Workbench/New UI/TUI：Proposal 治理状态继续保留 `approved`，
  实施轴显示 `rolled_back`；Outcome 存在或来源不可用时，issuer 在服务端对所有入口阻止再次签发 Contract。
  协议拒绝跨 Proposal 绑定和 `promoted/learning` 提权。详见 `HAR-09-6a-proposal-outcome-projection.md`。
- HAR-09.6b 已把原 Promotion Input 绑定的 Final Evaluation 和全部 HAR-08 H5c RED/GREEN lane 登记为
  `implementation_before_after`，并在每次读取时复验 Outcome、Proposal、Candidate 和 H5c authority。该 evidence
  不是 post-rollback 或长期结果，不授予 learning/promotion authority；它本身不能被后续恢复证据替代或改名。
- HAR-09.6c1 已在 active baseline installed slot 上重新执行 `--version` boot probe，并重新解析 launcher identity；
  Workbench/New UI/TUI 显示同一 verification。它只证明 installed runtime 的 fresh mechanical recovery，行为级 Eval、
  长期指标、promoted Outcome 和 supersede ledger 仍未完成。
- ARC-07.5f/5g 已补齐 exact installed backend 的 bounded JSON Eval channel 与 runtime-side platform/version
  identity：首个 runner 为 `protocol_hello@1`，Receipt 绑定 Suite/fixture、slot/manifest/backend bytes、真实子进程
  和经 target/version 复核的运行时身份。
- HAR-09.6c2a 已在上述 transport 上完成首个 Proposal/Outcome/6c1/BeforeAfter/original-H5c-bound 的单平台
  Behavioral Lane：exact installed baseline 按原 repetitions 生成 fresh H5a/H5c，unsupported runner 失败关闭，
  Agent Tool 与共享 Slash 同源。单 lane 不是完整矩阵，仍固定 `behavioral_evaluation_recorded=false`；详见
  `HAR-09-6c2a-post-rollback-behavioral-lane.md`。

EVO-02.1b 已把不可执行 Contract 包装为 workspace-bound durable Authority；HAR-09.5c 在其上补齐产品动作、
Proposal 单飞键和历史 projection 迁移。Contract 仍不是执行或 promotion 许可。

EVO-02.2a 已进一步把 Contract 绑定到持久、可恢复的唯一 Worktree Lease；Lease 仍不授予写 patch 或
执行检查的权限，且尚未暴露为用户动作，因此不改变 HAR-09 的 partial 结论。
