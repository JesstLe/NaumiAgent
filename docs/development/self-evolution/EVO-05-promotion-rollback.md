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
- [EVO-05.3f2b2b4 Fresh Interventional Attribution](EVO-05-3f2b2b4-fresh-interventional-attribution.md)：已交付；
  复验 Fresh H5c 并持久化目标指标归因，unchanged 不会误获 reflection eligibility。
- [EVO-05.3f2c1 Fresh Adversarial Sample](EVO-05-3f2c1-fresh-adversarial-sample.md)：已交付；在指定真实平台运行
  current-target RED 与 immutable-overlay GREEN probe pair，但尚未形成平台 cohort/matrix。
- [EVO-05.3f2c2 Fresh Adversarial Cohort](EVO-05-3f2c2-fresh-adversarial-cohort.md)：已交付；形成单一真实平台的
  连续 probe cohort、共享 Grant 和可恢复前缀，但尚未完成 required-platform matrix。
- [EVO-05.3f2c3a Fresh Adversarial Matrix Status](EVO-05-3f2c3a-fresh-adversarial-matrix-status.md)：已交付；动态区分
  completed/runnable/pending platform lane，只有全部 cohort 齐全才持久化完成矩阵，尚不授予远端调度或比较权威。
- [EVO-05.3f2c3b1 Required-Platform Dispatch Outbox](EVO-05-3f2c3b1-platform-dispatch-outbox.md)：已交付；为 runnable
  lane 形成 exact Worker/Job/Reservation 绑定的 durable queued Dispatch。
- [EVO-05.3f2c3b2a Authenticated Worker Claim](EVO-05-3f2c3b2a-authenticated-worker-claim.md)：已交付；通过
  supervisor-attested Ed25519 identity、一次性 challenge 和 lease hash chain 证明远端领取，并在 Worker epoch/Contract/reservation
  漂移后动态 fencing；尚不接收结果或写入 H5a。
- [EVO-05.3f2c3b2b1 Claim-Bound Execution Authorization](EVO-05-3f2c3b2b1-claim-bound-execution-authorization.md)：
  已交付；由父权限、current Claim、Runtime lease 和可撤销 Run Grant 派生完整远端执行范围，并支持 generation renewal、显式撤销
  与失败补偿；尚未接受 execution/result 事实。
- [EVO-05.3f2c3b2b2 Signed Result H5a Ingestion](EVO-05-3f2c3b2b2-signed-result-h5a-ingestion.md)：
  已交付；验证 exact Worker Ed25519 signature、typed result content digest、source/configuration/probe/Grant/lifecycle binding，支持
  durable admission 后的 H5a/pair prefix 恢复；仍不授予 cohort/Matrix/promotion authority。
- [EVO-05.3f2c3b2b3 Remote Platform Completion](EVO-05-3f2c3b2b3-remote-platform-completion.md)：
  已交付；从完整本地 prefix 生成 cohort，durable revoke execution authorization、释放 Worker capacity，并要求 Matrix 在事务内读取
  exact completion；仍不授予 comparison/rollout/promotion authority。
- [EVO-05.3f2c4 Fresh Adversarial Comparison](EVO-05-3f2c4-fresh-adversarial-comparison.md)：已交付；完整 matrix 后
  对每个平台从原始 H5a 重算 probe/identity/summary，并持久化 HAR-08 原生 H5b2/H5c。
- [EVO-05.3f2c5 Fresh Adversarial Attribution](EVO-05-3f2c5-fresh-adversarial-attribution.md)：已交付；逐平台复验
  H5c 并持久化领域正确的故障归因，安全 guardrail 保持不变不再被误判为候选无改善。
- [EVO-05.3f2d Fresh Final Evaluation](EVO-05-3f2d-fresh-final-evaluation.md)：已交付；覆盖 current source、Fresh
  Interventional 与 required-platform Adversarial 全部 cohort/H5c/attribution，负向结果不会开放重新审批。
- [EVO-05.3f3a Fresh Reapproval Authority](EVO-05-3f3a-fresh-reapproval-authority.md)：已交付；动态复验 eligible
  Fresh Final，禁止复用旧审批/签名，并要求专业角色重新签名和创建新交互。
- [EVO-05.3f3b1 Fresh Promotion Input](EVO-05-3f3b1-fresh-promotion-input.md)：已交付；版本化绑定 current Contract、
  Fresh Final 与 Reapproval Authority，只从旧 input 携带 patch/rollback 事实，不携带旧审批权威。
- [EVO-05.3f3b2 Fresh Approval Requirement](EVO-05-3f3b2-fresh-approval-requirement.md)：已交付；绑定原 Request 的
  target branch 与 Fresh Plan/Outcome 的 current revision/tree，强制新交互和专业角色新签名。
- [EVO-05.3f3b3 Fresh Role Interactions](EVO-05-3f3b3-fresh-role-interactions.md)：已交付；通过 HAR-10.6 创建新的
  role-scoped interaction/Response，专业回答只开放新签名资格，不复用旧 response/signature。
- [EVO-05.3f3b4 Fresh Professional Signatures](EVO-05-3f3b4-fresh-professional-signatures.md)：已交付；使用独立
  domain 的真实 Ed25519 challenge/receipt 绑定 Fresh Requirement/Response 与 current Principal event/key，旧签名不可复用。
- [EVO-05.3f3b5 Fresh Decision Aggregation](EVO-05-3f3b5-fresh-decision-aggregation.md)：已交付；机械聚合新 user consent、
  current 专业签名与 Fresh technical gates，形成 hash-chained Decision，只开放 staged rollout 输入资格。
- [EVO-05.4a Immutable Rollout Plan](EVO-05-4a-immutable-rollout-plan.md)：已交付；冻结 local canary、opt-in、
  percentage、stable 阶段 DAG、风险阈值和 rollback 约束，但不授予 stage-entry/执行权限。
- [EVO-05.4b1 Fenced Local-Canary Entry](EVO-05-4b1-fenced-local-canary-entry.md)：已交付；签发短期 local-canary scope，
  通过 HMAC-attested kill-switch generation 动态 fencing，仍不执行 canary。
- [EVO-05.4b2 Real Local-Canary Executor](EVO-05-4b2-local-canary-executor.md)：已交付；消费 immutable GREEN，
  通过 exact Profile、Run Grant、runtime lease 与 ARC-04 sandbox Worker 真实执行，并写入 crash-safe hash-chain journal。
- [EVO-05.5a Rollout Monitor Baseline](EVO-05-5a-rollout-monitor-baseline.md)：已交付；从 Fresh Final 绑定的
  Interventional GREEN raw H5a 冻结 latency、completion/error 与可信 cost 基线。
- [EVO-05.5b Runtime Observation](EVO-05-5b-runtime-observation.md)：已交付；将 terminal journal、baseline、
  frozen thresholds 与 HMAC control signals 聚合为 insufficient/passing/breached receipt。
- [EVO-05.5c Rollout Stage Completion Evidence](EVO-05-5c-rollout-stage-completion.md)：已交付；重验 current
  passing terminal prefix、Plan/Entry 与 control state，冻结 local-canary completion，但不授予 next-stage authority。
- [EVO-05.5d Durable Stage Advance Authorization](EVO-05-5d-stage-advance-authorization.md)：已交付；high/critical
  通过 durable user interaction 决定推进或拒绝，automatic path 只接受计划显式资格；短期 authority 绑定 control generation，
  仍不部署 candidate。
- [EVO-05.5e Candidate Bundle Admission](EVO-05-5e-candidate-bundle-admission.md)：已交付；验证 current rollback
  slot 与获批 baseline，消费 ARC-07.4b Ed25519 trusted-builder attestation 后真实 install/boot exact candidate bundle，
  并动态检测 pointer/bytes/trust-policy 漂移，但不执行 activation。
- [EVO-05.5f1 Local Opt-in Deployment Intent](EVO-05-5f1-opt-in-deployment-intent.md)：已交付；通过 HAR durable
  interaction 登记当前本机安装的显式 opt-in，冻结 exact previous-pointer CAS 与 candidate slot/boot/trust 绑定；
  不执行 pointer switch，不把本机 enrollment 虚报成全局 1% rollout。
- [EVO-05.5f2 Opt-in Activation and Reconciliation](EVO-05-5f2-opt-in-activation-reconciliation.md)：已交付；
  把 exact Intent authority 纳入 ARC-07 v2 pointer digest，执行 expected-pointer CAS，并在 Receipt 落盘前崩溃、
  甚至后续 rollback 后按历史 generation 机械补写；仍不启动进程或开放全局 percentage rollout。
- [EVO-05.5f3 Opt-in Runtime Launch and Health Receipt](EVO-05-5f3-opt-in-runtime-health.md)：已交付；通过
  stable launcher 真实启动 exact candidate health probe，以 lease/epoch fencing 收敛并发和崩溃重试，冻结
  healthy/unhealthy 终态；不启动用户 session、不完成 opt-in stage 或开放 percentage/stable authority。
- [EVO-05.5f4a Opt-in Runtime Liveness Window](EVO-05-5f4a-opt-in-liveness-window.md)：已交付；把 exact Health、
  Deployment/exposure、release binding 与 heartbeat ledger 聚合为 insufficient/passing/breached，但明确不把 heartbeat
  冒充 completed run。
- [EVO-05.5f4b Durable Opt-in Liveness Assessment](EVO-05-5f4b-durable-opt-in-liveness-assessment.md)：已交付；
  持久化 Window Receipt，以 HAR 有界分页重建 current assessment，并动态响应新增 failure 与 Deployment/pointer 漂移；
  不把 liveness 冒充用户任务成功。
- [EVO-05.5f4c Release-bound Execution Outcome Ledger](EVO-05-5f4c-release-bound-execution-outcome-ledger.md)：已交付；
  把 managed terminal run 的 release provenance、Completion Receipt、Run Usage 与 heartbeat coverage 冻结为 Outcome；
- [EVO-05.5f4d Opt-in Completed-run Aggregation](EVO-05-5f4d-opt-in-completed-run-aggregation.md)：已交付；
  动态聚合 current Outcome、passing liveness、GREEN Baseline 与 opt-in guardrails，形成独立 Stage Completion Evidence；
- [EVO-05.5f4e Opt-in Stage Advance Authorization](EVO-05-5f4e-opt-in-stage-advance-authorization.md)：已交付；
  通过 exact Completion、Plan、control 与 durable user decision 签发短期 `opt_in → percentage` Stage Entry authority；
- [EVO-05.5f5a Percentage Cohort Assignment](EVO-05-5f5a-percentage-cohort-assignment.md)：已交付；
  消费 signed population 与 installation proof-of-possession，确定性冻结 exact limited cohort；不执行部署或流量扩大；
- [EVO-05.5f5b Percentage Deployment Intent](EVO-05-5f5b-percentage-deployment-intent.md)：已交付；
  将 selected Assignment、verified inactive slot、Credential/host target 与 previous pointer CAS 冻结为一次性短期 Intent；
  不执行 boot、activation 或真实 exposure；
- [EVO-05.5f5c Percentage Boot Preparation](EVO-05-5f5c-percentage-boot-preparation.md)：已交付；
  以跨进程 claim/epoch 只执行一次 exact candidate boot probe，冻结 Prepared Receipt；不切换 pointer 或声明 deployment；
- [EVO-05.5f5d Percentage Activation Reconciliation](EVO-05-5f5d-percentage-activation-reconciliation.md)：已交付；
  将 Prepared content identity 写入 ARC-07 v2 pointer，执行 expected-pointer CAS，并可在 Receipt 写入崩溃、Intent 过期或
  后续 rollback 后从历史 generation 机械补写；不启动 runtime 或声明真实 exposure；
- [EVO-05.5f5e Percentage Runtime Launch and Exposure Receipt](EVO-05-5f5e-percentage-runtime-exposure.md)：已交付；
  将 5f5d Deployment 与 ARC-07.5e managed terminal identity、HAR starting→running observation chain 绑定，形成单 installation
  startup exposure；不声明用户请求、cohort health 或 rollout completion；
- [EVO-05.5f5f Percentage Runtime Observation Window](EVO-05-5f5f-percentage-observation-window.md)：已交付；
  绑定 exact Exposure startup pair，校验 origin 或 bounded suffix 的 heartbeat hash chain、连续运行时长、样本数、最大 gap 与
  stale 状态；只签发单 installation runtime-window authority，不声明 completed run 或 percentage stage completion；
- [EVO-05.5f5g Durable Percentage Observation Assessment](EVO-05-5f5g-durable-percentage-observation-assessment.md)：已交付；
  持久化 Window，以 HAR 有界分页验证长生命周期 suffix，并在 inspect 时动态响应 failure/stale、pointer 与 Exposure 漂移；
- [EVO-05.5f5h Percentage Release-bound Execution Outcome](EVO-05-5f5h-percentage-release-bound-execution-outcome.md)：已交付；
  冻结真实 terminal ChatRun 的 exact percentage release、Completion Receipt、Run Usage 与全运行区间 heartbeat coverage；失败
  结果也计入 cohort observation，但单 Outcome 不形成 stage completion；
- [EVO-05.5f5i Percentage Completed-run Aggregation](EVO-05-5f5i-percentage-completed-run-aggregation.md)：已交付；
  动态重验不同 run Outcome、passing Window、Plan 与 GREEN Baseline，计算完成率、错误率、p95 与成本门槛；只形成 percentage
  stage-completion evidence，不签发 stable entry；
- [EVO-05.5f5j Percentage-to-Stable Stage Advance](EVO-05-5f5j-percentage-to-stable-stage-advance.md)：已交付；
  通过 mandatory durable user option、exact Completion/Plan/control 与 TTL 签发 stable entry；不执行 stable deployment 或流量切换；
- [EVO-05.5f5k Stable Deployment Intent](EVO-05-5f5k-stable-deployment-intent.md)：已交付；
  以 current complete signed Population、逐安装 Ed25519 PoP、exact Admission、host target 与 previous pointer CAS 签发短期
  durable Intent；不执行 boot、activation、runtime 或声明 stable rollout；
- [EVO-05.5f5l Stable Boot Preparation](EVO-05-5f5l-stable-boot-preparation.md)：已交付；
  使用跨进程 claim/lease 真实探测 exact stable candidate，形成独立 Prepared Receipt；不切换 pointer 或启动用户进程；
- [EVO-05.5f5m Stable Activation Reconciliation](EVO-05-5f5m-stable-activation-reconciliation.md)：已交付；
  使用 Stable Prepared authority 执行 expected-pointer CAS，并从 immutable history 恢复 Receipt；不启动 runtime 或声明 rollout；
- [EVO-05.5f5n Stable Runtime Exposure](EVO-05-5f5n-stable-runtime-exposure.md)：已交付；
  将 exact Stable Deployment、managed runtime identity 与 HAR startup ledger 绑定为单 installation exposure；不声明持续健康、
  completed run、stable rollout 或 promotion；
- [EVO-05.5f5o Stable Runtime Observation Window](EVO-05-5f5o-stable-observation-window.md)：已交付；
  按 stable Plan 门槛验证单 installation heartbeat chain、duration、sample、gap 与 stale；不授予 population、completed-run、
  stable-stage 或 promotion authority；
- [EVO-05.5f5p Durable Stable Observation Assessment](EVO-05-5f5p-durable-stable-observation-assessment.md)：已交付；
  以 active Deployment 和 bounded HAR ledger 持久化并动态重验单 installation stable window；Intent 过期只禁止新启动，
  不截断已启动 runtime 的观察；仍不授予 Population、completed-run、stable-stage、rollout 或 promotion authority；
- [EVO-05.5f5q Stable Release-bound Execution Outcome](EVO-05-5f5q-stable-release-bound-execution-outcome.md)：已交付；
  将单个真实 terminal run 绑定到 exact Stable Intent、Population member、durable Window、Completion/Usage 与 heartbeat coverage；
  failed/cancelled 也进入 Population observation，但单 Outcome 不授予 stable-stage、rollout 或 promotion authority；
- [EVO-05.5f5s Stable Population Candidate Preview](EVO-05-5f5s-stable-population-candidate-preview.md)：已交付；
  有界校验 durable 5f5r receipts 并按 Snapshot/member 投影跨安装候选覆盖、缺失与冲突；生产动态重验尚未组合，固定不授予
  stable rollout 或 promotion authority；
- [EVO-05.5f5t Current Population Trust Reconciliation](EVO-05-5f5t-current-population-trust-reconciliation.md)：已交付；
  生产按需加载 Population Registry trust artifact，动态核对 Snapshot latest/trust/expiry 与 signed credential membership；仍不把
  current Population 冒充逐成员 5f5r current 或 stable rollout；
- [EVO-05.5f5u Dynamic Stage Completion Inspection Port](EVO-05-5f5u-dynamic-stage-completion-inspection-port.md)：已交付；
  通过 Runtime 只读端口逐 current member 调用现有 5f5r `inspect()`，有界形成 dynamic authority；默认生产 read graph 尚未自动组合，
  且 stable rollout/promotion authority 仍关闭；
- [EVO-05.5f5v Default Stable Read Graph](EVO-05-5f5v-default-stable-read-graph.md)：已交付；
  默认 Engine source-lazy 恢复 opt-in、percentage、stable 全部 inspect 依赖，拒绝 signer/interaction 写端口；真实动态预演可用，
  但不直接形成 rollout/promotion authority；
- [EVO-05.5f5w Stable Population Completion Authority](EVO-05-5f5w-stable-population-completion-authority.md)：已交付；
  writer-fenced exact member source-set 形成 durable Completion Receipt，并对 Snapshot/trust/member evidence 动态撤权；仍不授予
  stable rollout 或 promotion authority；
- [EVO-05.5f5x1 Stable Rollback Readiness](EVO-05-5f5x1-stable-rollback-readiness.md)：已交付；
  current Completion/Deployment 与真实 active/prior pointer、retained slot 和原始 Boot Receipt 形成 binary-only readiness；不预造
  breach-only Rollback Request，配置/数据、stable rollout 与 promotion authority 仍关闭；
- [EVO-05.5f5x2 Stable Rollout Authorization](EVO-05-5f5x2-stable-rollout-authorization.md)：已交付；
  member-scoped、短期、single-use、binary-only authority 绑定 Completion/Readiness/control generation；尚未执行 finalization，
  配置/数据和 promotion authority 关闭；
- [EVO-05.5f5x3 Stable Rollout Member Finalization](EVO-05-5f5x3-stable-rollout-member-finalization.md)：已交付；
  消费 exact Authorization，在 release store 内以 expected-pointer writer fence 完成单 member binary finalization，并可从落盘
  release event 恢复 Evolution Completion；Population rollout、配置/数据和 promotion authority 仍关闭；
- [EVO-05.6a Automatic Pause and Rollback Request](EVO-05-6a-automatic-pause-rollback-request.md)：已交付；
  exact breach 会触发或复用 kill switch，并冻结只读 exact Rollback Request，不虚报执行完成。
- [EVO-05.6b1 Immutable Rollback Source](EVO-05-6b1-immutable-rollback-source.md)：已交付；从 exact Git
  baseline 冻结只读 content-addressed bytes，检测 tree/blob 漂移，但不覆盖未部署 workspace。
- [EVO-05.6b2a Fenced Installed-Slot Rollback](EVO-05-6b2a-fenced-slot-rollback.md)：已交付；消费 exact
  Request/Source/Plan，以 v3 authority-bound pointer 和 expected-pointer CAS 回滚 binary bundle，支持 Receipt 写入
  崩溃恢复与 post-rollback Launch Resolution；需要配置/数据恢复时等待 ARC-07.6 并失败关闭。
- [EVO-05.6b2a1 Explicit Rollback Action](EVO-05-6b2a1-explicit-rollback-action.md)：已交付；Agent Tool 与
  `/evolution revalidation-rollback-execute` 共用 Engine 权限管线和同一执行服务，normal 单次确认、bypass 直接执行，
  New UI/TUI 显示同一 durable Receipt。
- EVO-05.6b2b Config/Data Rollback：消费 ARC-07.6 snapshot/migration authority，完成需要数据保护的兼容回滚。
- [EVO-05.7a Rollback Outcome Authority](EVO-05-7a-rollback-outcome-authority.md)：已交付；把真实 rollback
  Receipt 反向绑定到原始 Experiment Contract 与 Workbench Proposal，形成 `rolled_back` historical Outcome；长期指标、
  `promoted/superseded` 与 policy learning authority 仍保持关闭。
- EVO-05.7 后续：promoted/superseded ledger、HAR-08 before/after 与长期指标。

## 验收标准

- 未审批 package 无法进入主分支/稳定 channel，即使 bypass。
- rebase 后任何生产文件变化都使旧 Eval receipt stale。
- canary 超 guardrail 自动停止扩大并建议回滚，不删除诊断证据。
- rollback 在断电/进程崩溃中保持至少一个可启动版本。
- migration 不可逆时 promotion 必须阻断或提供前向恢复方案。
- 真实 patch 从 Proposal、审批、canary、监控到回滚完整演练。

## 当前边界

当前完成 EVO-05.1a/1b、EVO-05.2a-2d、EVO-05.3a-3f3b5 与 required-platform dispatch/claim/execution authorization。已有真实签名审批、target 前进后的隔离 replay/rebase、
Harness revalidation、旧证据失效、Fresh Interventional/Adversarial comparison 与 attribution、Fresh Final、Reapproval
Authority、版本化 Fresh Promotion Input、新 Approval Requirement/Response、专业 Ed25519 签名和 Fresh Decision。平台 lane
现可形成 exact Worker/capacity 绑定的 queued Dispatch，由持有 attested Ed25519 私钥的 Worker 领取，并从父权限派生短期远端执行授权；
已实现签名 result manifest、本地 H5a/pair prefix 摄取、remote cohort/Matrix 收口、immutable rollout plan、fenced local-canary entry、
真实 local-canary executor、可信 monitor baseline、runtime observation、显式本机 opt-in Intent、authority-bound pointer
activation/crash reconcile、percentage assignment/runtime/window/outcome aggregation、stable entry authorization、逐安装 stable
Deployment Intent/Boot Preparation/Activation/Runtime Exposure/Observation Window/Durable Assessment/Release-bound Outcome、逐成员 binary-only finalization 与 automatic pause/rollback request；无数据迁移的 version-slot rollback 已能
authority-bound 执行和崩溃对账，真实 rollback 也已形成 Proposal-bound `rolled_back` Outcome；但尚未实现 Population-level stable rollout aggregation、配置/数据 rollback、promoted/superseded Outcome 或长期指标回注。任何界面不得把 stable entry、Deployment Intent 或单 member finalization 宣称为完整 stable rollout，
也不得把 rollback source 冻结宣称为已回滚。
