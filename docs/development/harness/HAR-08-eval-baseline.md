# HAR-08 Eval Suite、Baseline 与回归比较

## 目标

建立可重复、可预算、可解释的评测系统，用于比较模型、Prompt、Tool、Harness、UI 协议和
自进化补丁。机械指标优先，LLM Judge 只能补充且必须记录模型/Prompt 版本。

## 子模块

- HAR-08.1 Eval schema：suite/case/input/fixture/expected/metrics/budget。
- HAR-08.2 Static runner：无模型的规则、Schema、知识索引与协议评测。
- HAR-08.3 Replay runner：复用 HAR-05，不执行副作用。
- HAR-08.4 Sandbox runner：临时目录/worktree 的真实 Tool 与 Check。
- HAR-08.5 Live runner：显式 `--live`、成本/时长上限、provider identity。
- HAR-08.6 Baseline identity：commit、config digest、model capability、platform。
- HAR-08.7 Comparator：绝对门槛、相对变化、置信区间、波动标记。
- HAR-08.8 CLI/Tool/API：`/harness eval|baseline` 与只读查询工具。

## 指标

完成率、verified rate、工具成功率、重复率、权限阻断率、token、成本、延迟、上下文峰值、
恢复成功率、UI 协议错误率。每个 case 指定主指标和 guardrail，禁止只优化单一总分。

## 验收标准

- 至少 5 个离线 fixture 和 1 个真实 NaumiAgent 小模块；重复两次结果可比较。
- 相同 baseline identity 不可被不同配置覆盖；变更必须创建新版本。
- Live 默认关闭；超预算立即停止并产出 partial receipt。
- 随机性 case 至少重复 5 次，报告均值、离散度和样本数。
- Eval 自身错误与被测实现失败分开分类。
- A4：macOS/Linux/Windows 的 Static/Replay 一致；Sandbox 差异有平台说明。

## 存储

在 H5 才新增 `harness_eval_results` 与 baseline 表，迁移幂等；大 artifact 只保存 URI/digest。

## 分阶段实现

- HAR-08.1a 离线协议 Eval：已实现。严格 Suite/Case schema、fixture SHA-256、生产 hello
  协议 runner、错误分类、`/harness eval` 与 `harness_eval` Tool 共用同一 Service；实施见
  `HAR-08-1a-offline-protocol-eval-design.md` 与
  `HAR-08-1a-offline-protocol-eval-implementation-plan.md`。
- HAR-08.3a Safe Replay Eval Runner：已实现。将 HAR-05 的既有持久 Replay baseline 映射为
  `safe_replay@1` typed Eval result；不调用模型、工具、Check 或 session，也不在 Eval 路径创建
  baseline。缺少 baseline、证据不完整或损坏都归类为 evaluation error，不冒充产品回归。
  `/harness eval replay [run-id|latest]` 与只读 `harness_eval_replay` Tool 共用 Service。详见
  `HAR-08-3a-safe-replay-eval.md`。
- HAR-08.6a Baseline Identity 契约：已实现。真实 Git HEAD/脏树 fingerprint、Suite/Profile/
  Runner 配置摘要、模型 capability contract、实际思考强度、平台与 Naumi 版本共同生成防篡改
  identity；脏树、未验证/不兼容能力和思考强度告警阻止 Baseline 晋升。实施与边界见
  `HAR-08-6a-baseline-identity-design.md`。
- HAR-08.6b Static Eval 身份闭环：已实现。Service 将真实 Profile digest/trust 注入离线 Eval，
  多 Suite 运行前后复核 Git fingerprint，Suite 原始摘要与 `model=null` identity 进入 typed result；
  Slash 与 Agent Tool 共享显示可晋升状态或稳定不可用原因。实施见
  `HAR-08-6b-static-eval-identity-surface.md`。
- HAR-08.7a Identity Compatibility Comparator：已实现。源码 revision 是 informational 差异；
  Eval/Profile/Runner/model 差异硬阻断；平台与当前 provisional 状态产生 caveat。详细规则见
  `HAR-08-7a-identity-compatibility-comparator.md`。
- HAR-08.7b Suite Mechanical Comparator：已实现。Identity gate 通过后计算 case transition、
  pass/failure 分类与机械 delta；fixture error、skip 和结构漂移返回 inconclusive，不伪装产品回归。
  详见 `HAR-08-7b-suite-mechanical-comparator.md`。
- HAR-08.7c Threshold/Guardrail Policy：已实现。严格 Suite policy 的 canonical digest 进入
  Identity；绝对/相对门槛与逐 case guardrail evidence 生成独立 Policy verdict，Eval error/skip
  永远 inconclusive。详见 `HAR-08-7c-threshold-guardrail-policy.md`。
- HAR-08.7d Statistical Comparator：已实现。每组至少 5 次，复用 Identity/结构 gate，计算均值、
  样本标准差、Student-t 95% CI 与 Welch 均值差区间；逐 case 组内摇摆优先标记 flaky，样本不足或
  Eval error 不形成产品结论。详见 `HAR-08-7d-statistical-comparator.md`。
- HAR-08.7e Quantitative Metric Observations：已实现。Typed Result 可保存有限数值、单位、方向、目标
  和唯一主指标；机械 Comparator、零回归 Policy、重复样本置信区间及 H5a Store 均保留数值证据。
  详见 `HAR-08-7e-quantitative-metric-observations.md`。
- HAR-08 H5a Eval Result Store：已实现。现有 HarnessStore schema v8 以 workspace/batch/suite/sample
  不可变键保存脱敏 typed Result、Identity 与内容摘要；幂等重试、冲突拒绝、迁移、隔离和篡改检测
  均已验证。详见 `HAR-08-H5a-eval-result-store.md`。
- HAR-08 H5b Baseline Version/Selector：已实现。schema v9 对合格 cohort 建立不可变单调版本，
  原子切换 workspace/suite active selector，并追加带摘要的 previous/current 审计事件；无 Identity、
  样本缺口、非全绿或未验证 guardrail 均不能晋升。详见 `HAR-08-H5b-baseline-version-selector.md`。
- HAR-08 H5b2 Comparison Reference Baseline：已实现。schema v16 为 H5b 记录增加 typed purpose；可信
  失败态 RED 可注册为只供 H5c 使用的 reference，但不切 active selector、不写 promotion event，且拒绝
  evaluation error/skipped/guardrail 失败。详见 `HAR-08-H5b2-comparison-reference-baseline.md`。
- HAR-08 H5c Comparison Receipt：已实现。schema v10 将 Baseline/Candidate 两组不可变样本、逐样本
  机械/Policy 证据和重复样本统计合成为防篡改权威 decision；Store 写入时复核 Baseline 与 Candidate
  的完整引用链。详见 `HAR-08-H5c-comparison-receipt.md`。
- HAR-08.8a Baseline Read Surface：已实现。`/harness baseline <suite-id>` 与 read-only
  `harness_eval_baseline` Tool 共享 Service，显示 active 版本和最近 Comparison receipt；新 UI、TUI
  与兼容终端复用同一 Slash 路由。详见 `HAR-08-8a-baseline-read-surface.md`。
- HAR-08.8b Repeated Eval Batch：已实现。显式 `--repeat 5..100` 或 `harness_eval_batch` 在单一
  source identity 边界运行一个声明 Suite，并把每个 sample 追加到 H5a immutable Store；普通单次 Eval
  保持只读。详见 `HAR-08-8b-repeated-eval-batch.md`。
- HAR-08.8c Explicit Promotion：已实现。Slash 与非只读 Agent Tool 共享 Service，以固定入口 actor、
  必填 reason 调用 H5b eligibility/版本/selector/审计事务；幂等重试不覆盖首次事实，旧版本重试不回拨。
  详见 `HAR-08-8c-explicit-baseline-promotion.md`。
- HAR-08.8d Active Baseline Comparison：已实现。Slash 与非只读 Agent Tool 编排 H5a/H5b/H5c，
  对完整 Candidate 生成幂等 receipt；selector 并发切换时保留真实引用并明确标记 stale。
  详见 `HAR-08-8d-active-baseline-comparison.md`。
- HAR-08.8e1 Typed Baseline 状态页：已实现。新 UI 通过 typed Bridge 请求并验证 active Baseline 与
  active-only Comparison snapshot；Textual TUI/兼容终端继续复用同一 Service 状态模型。
  详见 `HAR-08-8e1-typed-baseline-status-page.md`。
- HAR-08.8e2 Typed Eval Batch 真实进度：已实现。runner 逐样本报告真实评测完成，Service 区分
  evaluating/persisting，Bridge 非阻塞并发推送，新 UI 页面与 Textual TUI 状态栏同步显示。
  详见 `HAR-08-8e2-typed-eval-batch-progress.md`。
- HAR-08.8e3 引导式 Baseline 晋升：已实现。新 UI 与 Textual TUI 复用结构化理由选择和最终确认，
  显式 reason 与 Agent Tool 保持直达；所有路径最终只调用 H5b 权威 gate，并以 typed 状态说明 selector
  是否改变。详见 `HAR-08-8e3-guided-baseline-promotion.md`。
- HAR-08.4a 单项 Sandbox Profile Check Runner：已实现可信 Git snapshot、敏感路径阻断、三阶段 Profile
  复验、ARC-04.2/4.3 admission/执行链、源树防污染与独立 artifact。详见
  `HAR-08-4a-sandbox-profile-check-runner.md`。
- HAR-08.4b 生产 Sandbox Check Surface：已实现任务局部父回执、精确参数复核、Composer admission/finally
  cleanup、`/harness check` 与 Agent Tool 共路由，以及 ToolJob/lifecycle/artifact 可见证据。详见
  `HAR-08-4b-production-sandbox-check-surface.md`。
- HAR-08.4c 精确 Revision Snapshot：已实现完整 commit/tree authority、Git object blob 物化、manifest v2、
  非普通 blob/敏感路径阻断与终态复验，为 EVO baseline interventional runner 提供源字节前置。详见
  `HAR-08-4c-exact-revision-sandbox-snapshot.md`。
- HAR-08.4e Governed Sandbox Eval Check Group Kernel：已实现。把父权限/Run Grant 最终复验、成组 ARC-04
  admission、逐项 cleanup 与 lifecycle 完整性下沉到 Harness；Interventional RED/GREEN 已迁移复用。详见
  `HAR-08-4e-governed-sandbox-eval-check-group.md`。
- HAR-08.4f Resumable Sandbox Batch Coordinator：已实现。连续 H5a 前缀、单一 lease/grant、逐 sample
  Store-confirmed checkpoint、中断清理与跨 epoch 恢复已下沉到 Harness；Evolution cohort 仅保留兼容 adapter。
  详见 `HAR-08-4f-resumable-sandbox-batch-coordinator.md`。
- HAR-08.4g 有界 Sandbox Batch Admission：已实现。一个 Engine 内的 RED/GREEN/adversarial consumer 共用
  active/queued 硬上限；饱和、取消、嵌套自等待和完整 H5a 快速返回均有稳定语义。详见
  `HAR-08-4g-bounded-sandbox-batch-admission.md`。
- HAR-08.4h Native Sandbox Eval Request Authority：已实现。补齐原生 `sandbox` lane，并从受信 Profile、
  干净精确 Git revision、ordered checks、batch 与预算编译防篡改 request；执行前可机械拒绝 Profile 漂移。
  详见 `HAR-08-4h-native-sandbox-eval-request.md`。
- HAR-08.4i Native Sandbox Eval Service：已实现。`HarnessService` 复用 4h/4g/4f/4e，精确校验父权限，
  以 batch-scoped Run Grant 执行并即时写入 H5a；支持连续前缀恢复、外来 H5a 拒绝和稳定完成 receipt。
  详见 `HAR-08-4i-native-sandbox-eval-service.md`。
- HAR-08.4j Sandbox Eval Tool and Slash：已实现。Agent Tool 与共享 Slash 路由均通过唯一 Service，权限
  精确绑定 checks/samples/batch/run；真实 5-sample Worker batch 形成完整 H5a、artifact 与权限清理证据。
  详见 `HAR-08-4j-sandbox-eval-tool-slash.md`。
- HAR-08.4k Sandbox Eval Typed Progress：已实现。Coordinator checkpoint 经闭集 Runtime event 同步到
  New UI/TUI；两端展示真实 stage、Store-confirmed persisted、authority 与 run/grant 摘要，且 Sandbox 页面
  不伪造普通 Eval Case/Baseline 指标。详见 `HAR-08-4k-sandbox-eval-typed-progress.md`。
- HAR-08.4l Durable Sandbox Batch Admission：已实现。Harness Store v17 提供 workspace-wide FIFO、
  queued/active lease、崩溃回收、容量策略一致性和 owner/epoch fencing；生产 Harness/Evolution lane 共享同一
  durable authority。详见 `HAR-08-4l-durable-sandbox-admission.md`。
- HAR-08.4m Sandbox Admission Typed Progress：已实现。Store-confirmed queued position、capacity、
  admitted 与 terminal ticket state 经既有 Runtime event/Bridge 同步到 New UI/TUI；最终 completed 在容量
  槽释放后发布。详见 `HAR-08-4m-sandbox-admission-typed-progress.md`。
- HAR-08.4n Sandbox Admission Owner-fenced Cancel：已实现。exact ticket/authority/epoch/state
  fencing、durable accepted/rejected receipt、New UI/TUI 同权威与跨进程有界停止。详见
  `HAR-08-4n-sandbox-admission-cancel.md`。
- HAR-08.4o1 Sandbox Eval Request Manifest：已实现。Store v19 在权限与 Profile 复验后、admission
  之前持久化完整不可变 request；新进程可按 workspace/request SHA 恢复，同 batch 漂移、并发覆盖和
  持久内容篡改均失败关闭，为取消后的真实 retry 提供服务端 request authority。详见
  `HAR-08-4o1-sandbox-eval-request-manifest.md`。
- HAR-08.4o2 Sandbox Retry Intent Authority：已实现。Store v20 一次性消费 accepted cancel receipt，
  复验 source ticket 和 Request Manifest，生成新的 durable execution authority；并发、幂等、rejected
  audit 与 retry chain 均失败关闭。详见 `HAR-08-4o2-sandbox-retry-intent-authority.md`。
- HAR-08.4o3a Sandbox Retry Durable Dispatch：已实现。Store v21 原子 claim accepted retry intent
  与全新 admission ticket；live owner、expired crash recovery、terminal reconciliation、用户取消与
  owner/epoch/ticket fencing 均有 durable 语义。详见 `HAR-08-4o3a-sandbox-retry-dispatch.md`。
- HAR-08.4o3b Sandbox Retry Execution：已实现。新 retry permission、execution authority、ticket、
  Runtime lease 与 Run Grant 恢复原 Request Manifest 和连续 H5a；Tool 与 Slash 已通过真实隔离 Worker
  在 2/5 取消后续跑到 5/5。详见 `HAR-08-4o3b-sandbox-retry-execution.md`。
- HAR-08.4o3c Sandbox Retry UI Action：已实现。共享 protocol/Bridge 仍通过 retry Tool 执行，New UI
  可从 accepted cancel receipt 一键恢复，Textual TUI 复用 Slash 与 typed progress；receipt、dispatch、
  新 ticket 和 H5a 均机械校验。详见 `HAR-08-4o3c-sandbox-retry-ui-action.md`。
- HAR-08.4o3d Sandbox Retry Dispatch Catalog：已实现。Store v21 提供 workspace/filter/assessment
  绑定的 opaque cursor 与有界只读目录，逐项校验 retry receipt、Request Manifest、ticket fence 和连续
  H5a；共享 Tool/Slash 可审查 live/recovery/reconcile/terminal 状态。
  详见 `HAR-08-4o3d-sandbox-retry-dispatch-catalog.md`。
- HAR-08.4o3e Sandbox Retry Receipt-Bound Resume：已实现。accepted retry receipt 与 pending dispatch
  在同一事务落盘；新的 Permission receipt 精确绑定既有 dispatch/retry receipt/run，Store 只在 pending
  或 expired fence 下创建下一 generation ticket，并通过真实隔离 Worker 恢复原 Request Manifest/H5a，
  不重新消费 cancel receipt。详见 `HAR-08-4o3e-sandbox-retry-receipt-bound-resume.md`。
- HAR-08.4o3f Sandbox Retry Startup Recovery Snapshot：已实现。Bridge/TUI 启动时复用 open catalog
  做 20 项有界扫描，tamper-evident snapshot 不暴露 workspace/owner/authority；New UI/TUI 只显示
  人工恢复队列与精确共享 Slash，不自动 claim、续租或重放。真实 Git + SQLite + 新 Bridge 重启证明
  pending dispatch 未被改变。详见 `HAR-08-4o3f-sandbox-retry-startup-recovery-snapshot.md`。
- HAR-08.4o3g Sandbox Retry Dispatch Detail：已实现。Store 在单次只读连接中校验 dispatch、
  retry/cancel receipt、Request Manifest、ticket fence 与连续 H5a，并投影 tamper-evident snapshot、
  retention 保护引用和 receipt-bound resume 命令；共享 Tool/Slash 同时服务 New UI/TUI。
  详见 `HAR-08-4o3g-sandbox-retry-dispatch-detail.md`。
- EVO-03.6e 已证明 Adversarial RED/GREEN 也能复用同一 H5a、H5b2/H5c Store 与 comparator，Evolution
  只保留 lane authority gate，不复制 Harness 评分器；见
  `../self-evolution/EVO-03-6e-adversarial-h5c-comparison.md`。
- EVO-03.5c 已把同平台 Adversarial H5c 接入共享 Failure Attribution authority 与 durable Store；Harness
  仍只提供不可变结果和比较事实，不承担 Evolution 分类策略。见
  `../self-evolution/EVO-03-5c-adversarial-failure-attribution.md`。
- EVO-03.7a 通过 workspace-scoped comparison ID 重读 H5a/H5c，并只在 Evolution 层生成明确非最终的 Lane
  Receipt；Harness Store 新查询仍保持工作区隔离，不承担候选整体完成判断。见
  `../self-evolution/EVO-03-7a-evaluation-lane-receipt.md`。
- HAR-08.4 仍为 partial：跨主机 Batch admission、retry dispatch detail/retention，以及
  Linux/Windows CI 证据尚未完成。Live 与其余 surface 仍为 planned，当前不得把 HAR-08 整体标记为
  implemented。
