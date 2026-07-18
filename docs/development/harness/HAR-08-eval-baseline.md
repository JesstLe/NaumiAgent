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
- Sandbox/Live 与其余 surface 仍为 planned；其中 HAR-08.4 必须等待 ARC-04 提供可证明的隔离
  worker，不得复用会降级到本机执行的普通命令路径。当前不得把
  HAR-08 整体标记为 implemented。
- ARC-04.2b/2c 已提供 immutable ToolJob admission、dispatch 前 authority 复验、单调 lifecycle receipt 和
  unknown 副作用边界，但尚无执行 payload transport、Shell worker 或 OS sandbox；因此它们是 HAR-08.4 的
  真实前置，不是 Sandbox 完成证据。下一步只接一个真实 Profile check 的 non-PTY worker 垂直切片。
