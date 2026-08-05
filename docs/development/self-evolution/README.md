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

[EVO-GOV-01](EVO-GOV-01-agent-tool-permission-matrix.md) 已为 EVO-03.7、EVO-04.1-4.7 与
EVO-05.1a-05.3b1 的 durable Tool 建立精确权限规则：派生创建为中风险，append-only
Reflection 撤销和 Principal 治理为高风险动作；lockdown 阻断，bypass 全权限且无二次确认。以后新增非只读 Evolution Tool 必须与
权限规则和注册表门同一切片交付。

EVO-04.4a 已交付真实字节驱动的 Counterfactual Evidence：从 completed Independent Review 重读完整
authority 链，对受管 worktree 的 baseline/candidate/diff 与 Mutation Receipt 做逐文件复核，并检查更小
scope、删测试、metric/threshold、skip/mock 与评测泄漏。它不调用模型、不保存源码，也不形成最终 decision。

EVO-04.5a 已交付行为型 Reward-hacking Evidence：从 Counterfactual 重读 Final/Lane/Cohort authority，检查
真实任务退化、proxy divergence、平台选择性与资源换分；资源或平台证据不足时明确返回 `inconclusive`。

EVO-04.6a 已交付四态 Decision State：mechanical veto 固定 rejected，结构化 concern 固定 revise，证据不足或
高风险进入 escalated，全部 clear 才 accepted_experiment。EVO-04.6b 已让 escalated 通过 HAR-10.6 真实持久
交互形成不可变 Resolution；用户答案仍不能直接接受 Candidate 或执行 promotion。

EVO-04.7a 已交付 Reflection Memory：只保存确定性 lesson/action/signal 和 authority ID/digest，自定义用户文本、
Reviewer 叙事和源码不落库；记录不进入向量索引、自动召回或系统 Prompt，并支持 append-only 撤销。

[EVO-05.1a](EVO-05-1a-promotion-package-input-contract.md) 已交付 Promotion Package Input：仅 active 的
`accepted_experiment` Reflection 可冻结 patch、baseline、完整 receipt refs、risk、migration assessment 和
rollback plan。Reflection 被撤销后 Input 保留审计但动态失去 eligibility。

[EVO-05.1b](EVO-05-1b-promotion-package-contract.md) 已交付完整审查 Package：绑定 exact local target
branch、审批事实和 domain-separated signable digest；target 移动或 Reflection 撤销都会动态失效。
[EVO-05.2a](EVO-05-2a-approval-requirement-policy.md) 已交付 Approval Requirement：按 risk、protected
target/scope、migration 和 data backup 冻结 human roles、signature gates、technical gates 与 expiry；
[EVO-05.2a1](EVO-05-2a1-professional-role-signature-policy-v2.md) 已升级 current policy，要求所有非 user
专业角色提供真实 Ed25519 签名，同时保留 v1 artifact 可读。目标移动、
到期或 Reflection 撤销都会 fail closed。[EVO-05.2b](EVO-05-2b-approval-request-authority.md) 已把每个角色请求
接入 HAR-10.6 durable interaction，并冻结结构化回答、identity assurance 和独立 signature entry。专业角色在
身份/签名 authority 完成前不能计入 quorum。[EVO-05.2c1](EVO-05-2c1-approval-principal-authority.md) 已建立
HAR 人工确认的 Principal、角色与 Ed25519 公钥注册/轮换/撤销 authority；
[EVO-05.2c2](EVO-05-2c2-approval-signature-receipt-authority.md) 已通过带 nonce/expiry 的 domain-separated
Challenge 验证真实外部 Ed25519 signature，并让轮换、撤销、过期或 target 漂移后的历史 Receipt 动态失去未来
聚合资格。[EVO-05.2d](EVO-05-2d-approval-decision-aggregation.md) 已重读全部角色、签名、身份与技术门，形成
append-only 的 `approved|rejected|changes_requested|pending|stale` Decision Receipt；approved 只允许进入未来
rebase/revalidate。[EVO-05.3a](EVO-05-3a-revalidation-request-authority.md) 已把仍 current 的 approved Decision 与
exact Package 冻结为 deterministic、可动态失效的 Revalidation Request；
[EVO-05.3b1](EVO-05-3b1-exact-target-replay.md) 已从 active Lease 重读真实 Candidate bytes，并在一次性 detached
worktree 中完成 exact-target 写入、复核、清理和 durable Receipt。
[EVO-05.3b2a](EVO-05-3b2a-target-advance-authority.md) 已关闭 target 前进后 Request 永久不可执行的 authority 死锁：
仅 target 线性前进且审批/签名/Input/Reflection 其余部分仍 current 时授予隔离 rebase 资格；diverged 或任何非 target
证据失效仍阻断。[EVO-05.3b2b](EVO-05-3b2b-fenced-three-way-rebase.md) 已在 current target 的一次性 detached
worktree 执行真实三方合并，持久化 success/conflict/failure Outcome，并加入 claim、epoch fencing、过期恢复和残留
worktree 清理。[EVO-05.3c](EVO-05-3c-harness-revalidation-evidence.md) 已按 Replay/Rebase identity 重新构造受摘要约束的
source overlays，并通过 Harness Sandbox + ARC-04 Worker 执行当前受信任 Profile 的匹配检查，持久化新的 job/lifecycle
验证证据。[EVO-05.3d](EVO-05-3d-revalidation-outcome.md) 已原子写入旧 promotion authority 失效账本并签发
可动态 stale 的 Revalidation Outcome；通过只获得 staged-rollout candidate 资格，失败与来源漂移均 fail closed，
且 Final Evaluation、专业签名和 Approval 必须重新签发/聚合。
[EVO-05.3e](EVO-05-3e-fresh-evaluation-plan.md) 已把 current validated Outcome 机械转换成完整 fresh evaluation
matrix，明确要求重跑 Interventional、跨平台 Adversarial RED/GREEN、comparison、attribution 与 lane receipts；尚未
执行重评或开放 rollout。
[EVO-05.3f1](EVO-05-3f1-immutable-evaluation-source.md) 已把 exact target + revalidation overlay 捕获为经过 symlink、
size、digest 和 regular-file 防护的 content-addressed immutable blobs；后续 GREEN 评测可脱离旧 Lease worktree，
但完整重评仍未执行。
[EVO-05.3f2a](EVO-05-3f2a-revalidation-validation-plan.md) 已把旧 Experiment Contract 的 seed、预算、metrics 与旧
Final Evaluation 的 suite/sample/platform 策略重绑到 current-target RED 和 immutable-overlay GREEN，并以 current
Harness Profile 唯一覆盖每个文件的 required checks；该 authority 尚不执行评测或授予 promotion。
[EVO-05.3f2b1](EVO-05-3f2b1-runtime-source-pair.md) 已为 Harness Sandbox Eval 物化共享的同基线 RED/GREEN runtime
source pair，并以动态回调持续复验 Plan、Snapshot 与 blob bytes；旧 Interventional/Adversarial executor 尚未接线。
[EVO-05.3f2b2a](EVO-05-3f2b2a-fresh-runtime-contract.md) 已重新绑定真实 metric runner/version/fixture/timeout 与
current Profile adversarial probe coverage，并对缺 runner、probe 或预算形成 blocker。
[EVO-05.3f2b2b1](EVO-05-3f2b2b1-fresh-interventional-sample.md) 已真实执行一个 current-target RED/GREEN
Interventional sample pair，持久化 ARC-04 lifecycle、metric 与 phase-specific Run Grant evidence；完整 cohort、
comparison、Adversarial lanes、attribution 与 Final Evaluation 仍未完成。
[EVO-05.3f2b2b2](EVO-05-3f2b2b2-fresh-interventional-cohort.md) 已将 pair 扩展为连续 Fresh cohort，在一个
cohort-scoped Run Grant 与共享 batch admission 下运行全部 RED/GREEN 样本，并支持复验连续前缀后只恢复缺失后缀；
paired comparison、Adversarial lanes、attribution 与 Final Evaluation 仍未完成。
[EVO-05.3f2b2b3](EVO-05-3f2b2b3-fresh-interventional-comparison.md) 已从 ordered H5a 重新验证 Identity、metric/check
summary 并生成原生 H5b2/H5c；该 decision 仍不覆盖 Adversarial、attribution、Final Evaluation 或 promotion。
[EVO-05.3f2c1](EVO-05-3f2c1-fresh-adversarial-sample.md) 已在 Contract 指定且与 Worker 实际一致的平台执行一个
Fresh Adversarial RED/GREEN pair；platform cohort、跨平台 matrix 与 Adversarial H5c 尚未完成。
[EVO-05.3f2c2](EVO-05-3f2c2-fresh-adversarial-cohort.md) 已形成当前平台的连续 probe cohort，并支持中断后只恢复
缺失后缀；required-platform matrix 与各平台 H5c 尚未完成。
