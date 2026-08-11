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
[EVO-05.3f2b2b4](EVO-05-3f2b2b4-fresh-interventional-attribution.md) 已从 Fresh Interventional H5c 生成并持久化
机械归因；目标指标 unchanged 会要求 revise candidate，不会被 policy passed 掩盖。
[EVO-05.3f2b2b3](EVO-05-3f2b2b3-fresh-interventional-comparison.md) 已从 ordered H5a 重新验证 Identity、metric/check
summary 并生成原生 H5b2/H5c；该 decision 仍不覆盖 Adversarial、attribution、Final Evaluation 或 promotion。
[EVO-05.3f2c1](EVO-05-3f2c1-fresh-adversarial-sample.md) 已在 Contract 指定且与 Worker 实际一致的平台执行一个
Fresh Adversarial RED/GREEN pair；platform cohort、跨平台 matrix 与 Adversarial H5c 尚未完成。
[EVO-05.3f2c2](EVO-05-3f2c2-fresh-adversarial-cohort.md) 已形成当前平台的连续 probe cohort，并支持中断后只恢复
缺失后缀。[EVO-05.3f2c3a](EVO-05-3f2c3a-fresh-adversarial-matrix-status.md) 已对 required-platform matrix 建立
completed/runnable/pending 状态权威，且仅在全部 cohort 齐全后持久化完成矩阵；远端调度与各平台 H5c 尚未完成。
[EVO-05.3f2c4](EVO-05-3f2c4-fresh-adversarial-comparison.md) 已要求完整 matrix，并从各平台原始 H5a 重算
probe/identity/summary 后生成原生 H5b2/H5c；跨平台归因与新的 Final Evaluation 尚未完成。
[EVO-05.3f2c5](EVO-05-3f2c5-fresh-adversarial-attribution.md) 已逐平台复验 H5c 并持久化机械归因，且将
`passed + unchanged` 正确解释为 adversarial guardrail preserved；新的 Final Evaluation 尚未完成。
[EVO-05.3f2d](EVO-05-3f2d-fresh-final-evaluation.md) 已签发覆盖 current source、Fresh Interventional 与全部平台
Adversarial cohort/H5c/attribution 的新 Final Evaluation，并将证据完整与重新审批资格严格分离。
[EVO-05.3f3a](EVO-05-3f3a-fresh-reapproval-authority.md) 已增加动态重新审批门禁；只有 eligible Fresh Final 才能进入
后续 Approval Input，并强制旧审批/签名不可复用、专业角色重新签名。
[EVO-05.3f3b1](EVO-05-3f3b1-fresh-promotion-input.md) 已生成版本化 Fresh Promotion Input；它把旧 input 降为
patch/rollback 的只读基线，并原子绑定 current Contract、Fresh Final 与 Reapproval Authority，旧 Decision、Approval
和签名均不可复用。
[EVO-05.3f3b2](EVO-05-3f3b2-fresh-approval-requirement.md) 已从 current target authority 生成新的 Approval Requirement；
target branch 来自原 Request，revision/tree 来自 Fresh Plan/Outcome，所有角色必须创建新交互且专业角色重新签名。
[EVO-05.3f3b3](EVO-05-3f3b3-fresh-role-interactions.md) 已将每个 Fresh Requirement step 接入 HAR-10.6；user 的新回答
可形成 session-bound consent，专业角色回答只开放新签名资格，不能直接计入最终聚合。
[EVO-05.3f3b4](EVO-05-3f3b4-fresh-professional-signatures.md) 已为专业角色建立独立 domain 的真实 Ed25519 challenge/receipt；
签名绑定全新 Requirement/Response、current Principal event/key generation，换钥、撤销、角色或 target authority 漂移均失败关闭。
[EVO-05.3f3b5](EVO-05-3f3b5-fresh-decision-aggregation.md) 已聚合新 user consent、全部专业签名与五个 current technical
gates，形成 append-only Fresh Decision；只有动态 current 的 approved Decision 才开放 staged rollout 输入资格。
[EVO-05.4a](EVO-05-4a-immutable-rollout-plan.md) 已将 current approved Decision 与 exact Input/patch/migration/rollback
冻结为 local-canary→opt-in→percentage→stable 的风险分级计划；计划不授予 stage-entry 或执行权限。
[EVO-05.4b1](EVO-05-4b1-fenced-local-canary-entry.md) 已增加 HMAC-attested local-canary stage-entry 与 workspace kill switch；
pause/resume、expiry 或 Decision 漂移会动态 fence entry，且后续 stage/Git/publish 权限保持关闭。
[EVO-05.4b2](EVO-05-4b2-local-canary-executor.md) 已消费 immutable GREEN，在 exact current Profile、父权限、
Run Grant、runtime lease 与 ARC-04 sandbox Worker 下真实运行 local canary；append-only journal 支持 running 恢复、kill-switch
取消与 task cancellation 清理，但只开放 monitor authority。
[EVO-05.5a](EVO-05-5a-rollout-monitor-baseline.md) 已从 Fresh Final 指向的 exact Interventional GREEN raw H5a
冻结 duration/p95、completion/error 与 cost 基线；缺 raw sample 或 live cost evidence 会动态阻断 monitor input。
[EVO-05.5b](EVO-05-5b-runtime-observation.md) 已将 local-canary terminal journal 与 frozen baseline/threshold、
HMAC control signals 合并为 insufficient/passing/breached；breach 只开放 pause/rollback input，passing 不开放 stage advance。
[EVO-05.5c](EVO-05-5c-rollout-stage-completion.md) 已把 current passing evidence 冻结为 local-canary completion，
并机械区分 automatic-eligible 与 manual-interaction-required；next-stage/deployment authority 仍保持关闭。
[EVO-05.5d](EVO-05-5d-stage-advance-authorization.md) 已复用 Harness durable interaction，把用户 advance/decline、
Completion、control generation 与短期有效期冻结为 Stage Advance Receipt；只开放 opt-in entry，不执行部署。
[EVO-05.5e](EVO-05-5e-candidate-bundle-admission.md) 已将 exact candidate source-free bundle 真实安装到 immutable slot，
先验证 ARC-07.4b detached Ed25519 Build Attestation，再绑定获批 source provenance、rollback baseline 与 Boot Receipt；
key 撤销或 trust-policy 轮换会动态撤权，只开放 activation input，不切换 active pointer。
[EVO-05.5f1](EVO-05-5f1-opt-in-deployment-intent.md) 已通过 HAR durable interaction 登记当前本机安装的显式 opt-in，
冻结 exact previous-pointer CAS、candidate slot/boot 与 build trust 绑定；它不切换 pointer，也不把本机 enrollment
虚报成已执行全局 1% rollout。[EVO-05.5f2](EVO-05-5f2-opt-in-activation-reconciliation.md) 已把 exact Intent authority
写入 ARC-07 v2 pointer digest，执行 CAS activation，并可在回执写入崩溃甚至后续 rollback 后从历史 generation
机械补写相同 Receipt；它仍不启动进程、不开放 percentage/stable authority。
[EVO-05.5f3](EVO-05-5f3-opt-in-runtime-health.md) 已通过 stable launcher 启动 exact active candidate 的隐藏
health machine interface，以 claim lease/epoch fencing 收敛并发与 crash retry，并把真实进程终态冻结为
healthy/unhealthy Receipt；它明确不启动用户 session、不完成 opt-in stage，也不把单次本机探测虚报成 percentage rollout。
[EVO-05.5f4a](EVO-05-5f4a-opt-in-liveness-window.md) 已继续把 Health、显式 opt-in exposure、release identity 和
HAR heartbeat ledger 聚合为严格 liveness window；passing 仍固定 completed-run/stage-completion authority 为 false，
不把 heartbeat 冒充 completed run。
[EVO-05.5f4b](EVO-05-5f4b-durable-opt-in-liveness-assessment.md) 已持久化 Window Receipt，并通过 HAR 有界分页和动态
Health/Deployment/Binding 重验，在 inspect 时即时发现新增 failure 与 pointer 漂移；
[HAR-10.2k](../harness/HAR-10-2k-release-bound-chat-run-provenance.md) 已补齐真实 chat run 到 exact release 的不可变来源绑定，
[EVO-05.5f4c](EVO-05-5f4c-release-bound-execution-outcome-ledger.md) 已进一步组合终态回执、单次运行用量和完整 heartbeat
coverage，形成 release-bound Execution Outcome；[EVO-05.5f4d](EVO-05-5f4d-opt-in-completed-run-aggregation.md) 已按
current Outcome authority、passing liveness、GREEN Baseline 与冻结 guardrails 聚合独立 opt-in Stage Completion Evidence，
并对不可比较的零成本基线 fail closed。[EVO-05.5f4e](EVO-05-5f4e-opt-in-stage-advance-authorization.md) 已进一步通过
current Completion、Plan、control 与 durable user decision 签发短期 `opt_in → percentage` Stage Entry authority；它不冒充
percentage population assignment、deployment 或真实流量扩大。
[ARC-07.5c](../architecture/ARC-07-5c-signed-installation-population.md) 已补齐独立 Registry trust、隐私化安装凭证和完整
hash-chained population snapshot，使分桶拥有不可由本地客户端编造的 denominator；远端 Registry 服务仍是明确外部边界。
[EVO-05.5f5a](EVO-05-5f5a-percentage-cohort-assignment.md) 已将 current Stage Entry、Snapshot、Credential 与安装
proof-of-possession 组合为稳定 ranked cohort，精确冻结 denominator、target count、selected set 和 member rank；Snapshot、Plan
或 trust 更新会动态撤权。它仍不下载、激活或扩大真实流量；
[ARC-07.5d1](../architecture/ARC-07-5d1-signed-release-channel-catalog.md) 已提供 signed channel/build 双信任目录与 target
Resolution；[ARC-07.5d2](../architecture/ARC-07-5d2-verified-artifact-fetch.md) 已提供有界 HTTPS fetch、原子落盘、崩溃恢复和
动态撤权 Download Receipt；[ARC-07.5d3](../architecture/ARC-07-5d3-verified-archive-admission.md) 已完成安全解包、构建证明重验和
immutable inactive-slot Admission。
[EVO-05.5f5b](EVO-05-5f5b-percentage-deployment-intent.md) 已将 selected Assignment、exact Admission、Registry Credential、
当前主机 target 与 previous pointer CAS 冻结为一次性 5 分钟 Intent；它不重复逐安装询问，不执行 boot、pointer switch 或
真实 exposure。[EVO-05.5f5c](EVO-05-5f5c-percentage-boot-preparation.md) 已以 durable claim/lease 执行 exact candidate
`--version` probe 并形成 Prepared Receipt；[EVO-05.5f5d](EVO-05-5f5d-percentage-activation-reconciliation.md) 已将 Prepared
identity 写入 ARC-07 v2 pointer、执行 expected-pointer CAS，并支持从历史 generation 恢复 Deployment Receipt。它仍未启动
用户 runtime 或记录真实 percentage exposure。[EVO-05.5f5e](EVO-05-5f5e-percentage-runtime-exposure.md) 已进一步将生产
TerminalRuntimeLifecycle 的 managed release binding 与 starting→running observation 绑定为单 installation Exposure Receipt；
它不把 terminal startup 冒充用户请求或 cohort rollout completion。
[EVO-05.5f5f](EVO-05-5f5f-percentage-observation-window.md) 已继续从 exact Exposure origin 验证完整 heartbeat hash chain，
按冻结 percentage guardrail 区分 insufficient/passing/breached，并在 stopped/failed/gap/stale 时停止签发 runtime-window
authority；它仍固定 completed-run、cohort、stage-completion、stable 与 promotion authority 为 false。
[EVO-05.5f5g](EVO-05-5f5g-durable-percentage-observation-assessment.md) 已完成该 durable assessment：支持最多 5000 条
verified suffix、跨 Service 并发幂等，并在 failure/stale、pointer 或 Exposure 漂移时动态撤权。
[EVO-05.5f5h](EVO-05-5f5h-percentage-release-bound-execution-outcome.md) 已进一步冻结真实 terminal ChatRun 的 exact
percentage release、Completion Receipt、Run Usage 与完整 heartbeat coverage；失败结果也保留为 cohort observation input，且
单 Outcome 不具有 stage-completion authority。
[EVO-05.5f5i](EVO-05-5f5i-percentage-completed-run-aggregation.md) 已动态聚合不同 run ID，并按 GREEN Baseline 计算 error、
completion drop、p95 latency 与 cost guardrail；来源漂移会撤权，且 Stage Completion 仍不等于 stable entry 或 rollout 完成。
[EVO-05.5f5j](EVO-05-5f5j-percentage-to-stable-stage-advance.md) 已把 current passing Completion、Plan、control generation、
mandatory user option 与短期 TTL 冻结为 stable entry authority；bypass 不绕过该治理门，且 entry 仍不等于 stable deployment。
[EVO-05.5f5k](EVO-05-5f5k-stable-deployment-intent.md) 已交付：stable 以 current complete signed Population 和逐安装
Ed25519 proof-of-possession 覆盖 100% 目标，不复用 percentage selected cohort；durable Intent Service/Store/View 绑定 exact
Admission、host target 与 previous pointer CAS，仍不执行 boot 或声明 stable rollout。
[EVO-05.5f5l](EVO-05-5f5l-stable-boot-preparation.md) 已交付：以跨进程 claim/lease 对 exact stable candidate 执行真实
`--version` probe，形成独立 Stable Prepared Receipt 和 activation input；仍不切换 pointer、不启动用户进程或声明 stable rollout。
[EVO-05.5f5m](EVO-05-5f5m-stable-activation-reconciliation.md) 已交付：以 Stable Prepared authority 和 frozen previous pointer
执行原子 CAS，并可在 Receipt 落盘崩溃后从 activation history 恢复；仍不启动 runtime、不记录 exposure 或声明 stable rollout。
[EVO-05.5f5n](EVO-05-5f5n-stable-runtime-exposure.md) 已交付：将 current Stable Deployment 与 ARC-07.5e managed runtime
identity、HAR starting/running ledger exact 绑定，形成单 installation startup exposure；runtime stopped 后保留历史 fact 并动态
撤销 observation input，仍不声明持续健康、完整 population rollout 或 promotion。
[EVO-05.5f5o](EVO-05-5f5o-stable-observation-window.md) 已交付：从 exact Exposure 与 HAR sample chain 机械计算 stable 阶段
持续时长、样本、gap 和 stale，区分 insufficient/passing/breached；它仍是单 installation artifact，不授予 completed-run、
population、stable completion 或 promotion authority。
[EVO-05.5f5p](EVO-05-5f5p-durable-stable-observation-assessment.md) 已交付：从 HAR Store 有界重建 current stable window，
append-only 持久化并在 inspect 时动态复核 active Deployment、Binding、ledger head 与 pointer；Intent TTL 只限制新启动，不截断
已启动 exact runtime 的观察能力。它仍不授予 Population、completed-run、stable-stage、rollout 或 promotion authority。
[EVO-05.5f5q](EVO-05-5f5q-stable-release-bound-execution-outcome.md) 已交付：将真实 ChatRun 的 release provenance、Completion
Receipt、RunUsage 与 heartbeat coverage 绑定到 exact Stable Intent、Population member 和 durable Window；失败结果也进入 Population
observation，只有 completed 形成单 run authority，仍不授予 stable-stage、rollout 或 promotion authority。
[EVO-05.5f5r](EVO-05-5f5r-stable-completed-run-aggregation.md) 已交付：动态重验同一 Stable Intent 的不同真实 run，按 current
stable Window、GREEN Baseline 和第四阶段 guardrail 计算 completed-run aggregation；它只形成单 installation member 的
stable-stage completion，仍不冒充完整 Population rollout 或 promotion。
[EVO-05.5f5s](EVO-05-5f5s-stable-population-candidate-preview.md) 已交付：只读、有界校验并聚合每个 Intent 最新 5f5r
receipt，显示跨成员 passing/breached/insufficient、缺失与 lineage/重复 Intent 冲突；Agent Tool 与共享 Slash 同源，
但在生产动态 5f5r 重验组合完成前固定无 stable rollout/promotion authority。
[EVO-05.5f5t](EVO-05-5f5t-current-population-trust-reconciliation.md) 已交付：生产 Engine 按需加载 installer-owned Population
Trust Policy，并将 current/latest/trust/expiry 与 signed credential membership 对账投影到同一预演；Snapshot current 仍不等于
逐 member 5f5r current，因此 dynamic/rollout/promotion authority 继续关闭。
[EVO-05.5f5u](EVO-05-5f5u-dynamic-stage-completion-inspection-port.md) 已交付：Runtime 可注入现有 5f5r Service 的只读
`inspect()`，预演按 current member 分批动态重验并严格核对完整 receipt identity；缺端口或任一成员失权均失败关闭，且仍不授予
stable rollout/promotion authority。
[EVO-05.5f5v](EVO-05-5f5v-default-stable-read-graph.md) 已交付：默认 Engine 以 source-lazy factory 恢复完整 opt-in → percentage
→ stable 只读 evidence graph；空候选不读 trust/DB，外层不暴露 assess/write，真实 candidate 使用原 5f5r `inspect()` 动态重验。
[EVO-05.5f5w](EVO-05-5f5w-stable-population-completion-authority.md) 已交付：冻结 exact current signed Population 与全部
member 5f5r source，在 SQLite writer fence 内幂等签发 durable Completion Receipt；历史 Receipt 不重写，但 Snapshot/trust/member
source 或动态重验变化会立即撤权。stable rollout 与 promotion authority 仍关闭。
[EVO-05.5f5x1](EVO-05-5f5x1-stable-rollback-readiness.md) 已交付：将 current Completion、current Stable Deployment、
真实 active pointer、retained prior slot 与原始 Boot Receipt 绑定为 content-addressed binary rollback readiness；配置/数据、stable
rollout 与 promotion authority 仍关闭，健康 rollout 不误用 breach-only Rollback Request。
[EVO-05.5f5x2](EVO-05-5f5x2-stable-rollout-authorization.md) 已交付：按 member 将 Completion、Readiness 与
kill-switch generation 冻结为短期 single-use binary-only Authorization；配置/数据与 promotion authority 关闭，尚未执行 finalization。
[EVO-05.6a](EVO-05-6a-automatic-pause-rollback-request.md) 已让 exact breach 幂等触发或复用 HMAC kill switch，并冻结
绑定 exact prior Rollback Plan 的只读 Request；它不写 workspace/Git，也不把请求虚报成已回滚。
[EVO-05.6b1](EVO-05-6b1-immutable-rollback-source.md) 已从 exact Git commit/tree 读取 baseline blob，验证每个
restore/remove step 并写入只读 content-addressed storage。
[EVO-05.6b2a](EVO-05-6b2a-fenced-slot-rollback.md) 与
[EVO-05.6b2a1](EVO-05-6b2a1-explicit-rollback-action.md) 已完成 authority-bound version-slot CAS、boot/launch
验证、崩溃对账，以及 normal 单次确认/bypass 直通的 Agent Tool 与共享 Slash 入口。
[EVO-05.7a](EVO-05-7a-rollback-outcome-authority.md) 已把真实 rollback Receipt 反向绑定到原始 Experiment Contract
与 Workbench Proposal，形成动态可撤权的 `rolled_back` Outcome；长期指标和 promoted/superseded 状态尚未完成。
[EVO-06.1a](EVO-06-1a-outcome-backed-opportunity-discovery.md) 已把该真实 `rolled_back` Outcome 确定性回注
现有 Candidate Store：同源并发幂等、同根失败聚合，Review/Workbench 入队前动态重验来源 authority；它不复制源码/补丁，
也不授予实验、学习或推广权限。accepted/promoted Outcome 与后续 Capability Proposal/Shadow/Activation 仍未完成。

[EVO-05.5f5x3a](EVO-05-5f5x3a-authenticated-remote-readiness-claim.md) 已为跨安装 Stable Finalization
补齐 Population Credential-bound challenge、Ed25519 assertion 与 durable authenticated claim；它不把远端签名声明冒充
runtime revalidation，也不授予 readiness、execution、rollout 或 promotion authority。
[EVO-05.5f5x3b](EVO-05-5f5x3b-remote-release-store-probe.md) 已让目标安装真实重读 active/candidate/previous/rollback
pointer、slot 与 Boot Receipt，以 ARC-07.5c1 installation key 签署 fresh source digest；Control Plane 只形成短期 binary
readiness，不授予 remote execution、stable rollout、配置/数据或 promotion authority。
[EVO-05.5f5x3c](EVO-05-5f5x3c-signed-remote-finalization-authorization.md) 已让独立 Rollout Control key 对 current x3b
Probe 签发逐 member、短期、single-use、binary-only portable Finalization Authorization，并由 installer-owned Trust Policy
验签。[EVO-05.5f5x3d](EVO-05-5f5x3d-remote-stable-member-finalization-executor.md) 已进一步形成 signed Execution Grant，
让目标重验真实 Release Store、执行 expected-pointer CAS、签署 Result，并由 Control Plane 重验后记录 Receipt；自动 transport、
超期恢复与 Population aggregation 仍未完成。
[EVO-05.3f2c3b1](EVO-05-3f2c3b1-platform-dispatch-outbox.md) 已把实时准入的 required-platform Worker lane 转为 durable
queued dispatch，并在 exact Worker incarnation 上预留容量。[EVO-05.3f2c3b2a](EVO-05-3f2c3b2a-authenticated-worker-claim.md)
已增加 supervisor-attested Ed25519 Worker Identity、一次性 claim challenge 和可续期 lease hash chain。
[EVO-05.3f2c3b2b1](EVO-05-3f2c3b2b1-claim-bound-execution-authorization.md) 已将 current Claim 与父权限、Runtime lease、
可撤销 Run Grant 和 exact evaluation scope 绑定。[EVO-05.3f2c3b2b2](EVO-05-3f2c3b2b2-signed-result-h5a-ingestion.md)
已接收 exact Worker Ed25519-signed typed result prefix，并在本地重算后幂等写入 H5a/pair Store。
[EVO-05.3f2c3b2b3](EVO-05-3f2c3b2b3-remote-platform-completion.md) 已原子收口 authorization/capacity、生成 cohort，
并以 completion 门禁推动 Matrix lane 完成；远端 stable runtime、配置/数据 rollback 与完整长期 Outcome 仍未完成。
[HAR-09.6d1](../harness/HAR-09-6d1-post-rollback-long-term-observation-contract.md) 已把 recovered Matrix、fresh
Runtime Verification、baseline slot/pointer/binary 与长期窗口规则冻结为不可变契约；真实 runtime binding admission、
heartbeat window 聚合已由 6d2/6d3 继续完成，promoted Outcome 仍未完成。
[HAR-09.6d2](../harness/HAR-09-6d2-post-rollback-runtime-observation-admission.md) 已继续将该契约逐字段绑定到 exact
managed runtime identity 与 startup-origin Harness ledger；6d3 已完成长期窗口评估，promoted Outcome 仍未完成。
[HAR-09.6d3](../harness/HAR-09-6d3-post-rollback-long-term-observation-assessment.md) 已完成真实 ledger 分页、head
对账、四态长期窗口和动态撤权。[HAR-09.6e1](../harness/HAR-09-6e1-post-rollback-long-term-outcome-authority.md)
已进一步签发 rollback-recovery-observed Outcome revision 与 append-only supersede event，保留 immutable rollback fact；
[HAR-09.6e2](../harness/HAR-09-6e2-long-term-outcome-projection-parity.md) 又通过 Projection v2 同源同步
Workbench/New UI/Textual TUI，并保持 Contract 终态阻断。promoted Outcome、配置/数据 rollback 与 policy learning
authority 仍未完成。
