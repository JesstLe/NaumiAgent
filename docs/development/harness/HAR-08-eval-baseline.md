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
- Replay/Sandbox/Live、Baseline promote 与完整存储仍为 planned；当前不得把
  HAR-08 整体标记为 implemented。
