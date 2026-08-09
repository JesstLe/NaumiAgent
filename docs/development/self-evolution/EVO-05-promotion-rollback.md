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
- [EVO-05.6a Automatic Pause and Rollback Request](EVO-05-6a-automatic-pause-rollback-request.md)：已交付；
  exact breach 会触发或复用 kill switch，并冻结只读 exact Rollback Request，不虚报执行完成。
- [EVO-05.6b1 Immutable Rollback Source](EVO-05-6b1-immutable-rollback-source.md)：已交付；从 exact Git
  baseline 冻结只读 content-addressed bytes，检测 tree/blob 漂移，但不覆盖未部署 workspace。
- EVO-05.6b2 Rollback Executor：消费 ARC-07 version slot，执行 binary/config/schema/patch 的 crash-safe 兼容回滚、
  数据保护和启动验证。
- EVO-05.7 Outcome record：promoted/rolled_back/superseded 与长期指标。

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
activation/crash reconcile 与 automatic pause/rollback request，但尚未实现 opt-in runtime health/completion、全局 percentage
assignment、version-slot rollback executor 或最终 Outcome 回注。任何界面不得把单机 opt-in 宣称为全局 1% rollout，
也不得把 rollback source 冻结宣称为已回滚。
