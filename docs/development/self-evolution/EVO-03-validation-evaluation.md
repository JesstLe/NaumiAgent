# EVO-03 多层验证与 Eval 对照

## 目标

证明变异不仅“能编译”，还在目标指标上优于 baseline 且没有破坏 guardrail；所有结论由 HAR-08
Eval 和真实 Harness 检查产生。

## 子模块

- EVO-03.1 Validation plan：按改动语言/模块选择 lint、compile、unit、contract、smoke。
- EVO-03.2 Baseline run：相同 fixture/环境/预算先测 baseline。
- EVO-03.3 Candidate run：隔离 worktree、相同 seed/order/resource。
- EVO-03.4 Comparator：主指标改善、guardrail 无退化、噪声/置信度。
- EVO-03.5 Failure attribution：candidate defect、environment、eval error、flaky/unknown。
- EVO-03.6 Adversarial suite：边界、并发、安全、恢复、跨平台、奖励投机探针。
- EVO-03.7 Evaluation receipt：before/after、sample、成本、失败、artifact digest。

## 验收标准

- baseline/candidate 环境 identity 不同则结果不可直接比较。
- 定向测试必须包含新 RED/GREEN；仅 import 成功永远不足。
- 主指标改善但安全/正确性 guardrail 退化时自动拒绝。
- flaky case 重复并报告分布，不允许挑选最好一次。
- Eval runner 自身失败不算 candidate 失败，进入 blocked/needs_rerun。
- A4：至少一个真实 NaumiAgent 小模块改进在 macOS/Linux 比较；平台特有变更有专属矩阵。

## 已实现切片

- EVO-03.1a Validation Plan Core：已实现。把 Contract、active Lease、Source Snapshot 与 Mutation Receipt
  v2 机械绑定为防篡改、不可执行计划；固定同 fixture/seed/environment 的 RED→GREEN 指标对，并按文件
  类型声明 lint/compile/unit/contract/smoke 要求。详见 `EVO-03-1a-validation-plan-core.md`。
- EVO-03.1b Validation Profile Check Binding：已实现。按 changed path 将每类要求唯一绑定到当前、用户
  信任的 Harness Profile check，artifact 只保存 spec/argv digest 并要求执行前重验 trust；同时修复深层
  `**` changed-path 匹配。详见 `EVO-03-1b-validation-profile-binding.md`。
- EVO-03.2a Baseline Cohort Request：已实现。引用 Plan/Binding 固定 HAR-08 RED Suite/Batch、至少 5 次
  样本、确定性 seed、baseline/environment identity、check coverage 与总预算；保持不可执行并要求 ARC-04
  物化。详见 `EVO-03-2a-baseline-cohort-request.md`。
- EVO-03.2b Metric Runner / Timeout Binding：已实现。`self_review_static@1` 绑定真实 AST scanner，HAR-05
  replay 绑定现有 `safe_replay@1` 并在缺精确 fixture 时阻断，feedback recurrence 在缺 observation-window
  runner 时阻断；所有可执行 metric timeout 纳入 cohort 总预算。详见
  `EVO-03-2b-metric-runner-binding.md`。
- HAR-08.7e Quantitative Metric Observations：已作为跨模块前置实现。finding count 等数值现在能进入
  typed Result、方向机械比较、Policy、重复样本置信区间与 H5a Store。审计同时确认 Safe Replay
  非干预，不能代替 baseline/candidate 代码执行。
- EVO-03.2c Self-Review Static RED Baseline：已实现。从精确 Git commit/tree 的 object database 读取
  Plan Python blob，实际重复 AST 扫描并把 finding count 以连续 sample 写入 H5a；执行前重验 Profile
  trust，支持一致前缀续写和防篡改完成回执，不运行项目代码或冒充 ARC-04。详见
  `EVO-03-2c-self-review-static-red-baseline.md`。
- EVO-03.2d Validation File Operation Binding：已实现。Validation Plan v2 防篡改地继承 Mutation Receipt
  的 modify/create、baseline before 与 candidate after digest；v1 保持只读兼容但不能执行。RED executor
  现可安全表示新建文件的空 baseline，并重验 modify blob digest。详见
  `EVO-03-2d-validation-file-operation-binding.md`。
- EVO-03.3a Self-Review Static GREEN Cohort：已实现。重新读取 active Lease/Trust/H5a RED evidence，精确
  校验受管 worktree 的 HEAD、branch、dirty path、operation 与 candidate after digest，在不可变临时快照
  中重复相同 AST scanner，并将连续 GREEN sample 写入独立 H5a batch。支持防漂移、断点续写、幂等重放
  与防篡改完成回执；不运行项目代码。详见 `EVO-03-3a-self-review-static-green-cohort.md`。
- EVO-03.4a Self-Review Quantitative Comparison：已实现。完整重验 RED/GREEN/Plan/Binding authority 与 H5a
  ordered digest，使用 H5b2 comparison reference 接入原生 H5c，保留 statistical/mechanical/Policy verdict，
  不另造 Evolution 评分管线。详见 `EVO-03-4a-self-review-quantitative-comparison.md`。
- EVO-03.5a Failure Attribution Contract：已实现。把 H5c decision/statistical/policy/mechanical codes 机械映射
  为 candidate defect、runner infrastructure、environment incompatible、flaky、evidence incomplete 或 objective
  unchanged，生成防篡改 typed receipt 并幂等持久化。详见 `EVO-03-5a-failure-attribution-contract.md`。
- EVO-03.2e Interventional RED Check Sample：已实现。重新验证 Plan/Profile/Request/Metric authority 与当前
  Profile trust，在精确 baseline commit/tree 上通过可撤销 Run Grant 和 ARC-04 Worker 执行单个 sample 的
  全部 Profile checks，写入既有 H5a，并绑定每项 lifecycle receipt digest；明确不执行 metric、不冒充完整
  cohort。详见 `EVO-03-2e-interventional-red-check-sample.md`。
- EVO-03.2f Interventional RED Metric Sample：已实现。在 H5a 不可变键首次写入前，把 ARC-04 Profile-check
  cases 与精确 baseline Git objects 上真实运行的 `self_review_static@1` typed observations 合为一个复合
  Suite；Suite identity 绑定全部 authority，幂等读取复验 case/metric/lifecycle 完整性。详见
  `EVO-03-2f-interventional-red-metric-sample.md`。
- EVO-03.2g Interventional RED Cohort：已实现。单次执行尝试以一个 Runtime lease/Run Grant 编排连续
  H5a samples，支持跨父回执 300 秒窗口、中断前缀保留、新 epoch/grant 续跑、完整 authority/lifecycle
  复验与防篡改 completion receipt。详见 `EVO-03-2g-interventional-red-cohort.md`。
- EVO-03.3b Interventional GREEN Request：已实现。把完整 RED receipt、Plan/Profile/Metric authority、
  active Experiment Lease 与 candidate provenance 冻结为不可执行请求；保持相同 suite/seed/order/预算，
  不提前授予 Shell authority。详见 `EVO-03-3b-interventional-green-request.md`。
- EVO-03.3c1 Shared Candidate Snapshot：已实现。静态 GREEN 与后续 interventional GREEN 共用唯一的
  Lease/Git/status/file digest/fingerprint 捕获边界，并支持执行后漂移复验。详见
  `EVO-03-3c1-shared-candidate-snapshot.md`。
- EVO-03.3c2b2 Interventional GREEN Single Sample：已实现。在重验完整 RED H5a cohort、active Lease、
  Profile trust、同平台/同 configuration 与候选 Snapshot 后，用精确 baseline + typed candidate overlays
  通过共用 ARC-04 kernel 执行同 checks 和 metrics；支持幂等防漂移回执。详见
  `EVO-03-3c2b2-interventional-green-sample.md`。
- EVO-03.3c2c1 Shared Interventional Cohort Governance Kernel：已实现。把连续 H5a 前缀、cohort-scoped
  Runtime lease/Run Grant、跨 epoch 恢复、sample receipt 对齐与异常清理收敛为 RED/GREEN 共用内核；RED
  已迁移且行为保持。详见 `EVO-03-3c2c1-shared-interventional-cohort-kernel.md`。
- EVO-03.3c2c2 Interventional GREEN Continuous Cohort：已实现。每轮一次完整 preflight 后，以共享 cohort
  kernel 编排连续 candidate samples；支持中断前缀、新 epoch/grant 恢复、同 candidate/config/platform
  evidence gate、防漂移幂等 completion receipt。详见 `EVO-03-3c2c2-interventional-green-cohort.md`。
- EVO-03.4b Interventional RED/GREEN H5c Comparison：已实现。完整重验两组 completion authority、ordered
  H5a、identity/config/platform、check/metric/lifecycle/grant 与 summary evidence 后，复用共享 H5b2/H5c
  kernel 持久化原生 mechanical/Policy/statistical verdict。详见
  `EVO-03-4b-interventional-h5c-comparison.md`。
- HAR-08.4a/4b/4c 与 ARC-04.3a/3b 已实现带生产权限委托、精确 baseline commit/tree 的单项真实 Sandbox
  Profile check，但 Request check/sample 编排与 candidate 对照尚未实现，因此不计为 EVO-03 interventional lane 完成。

UI-12.3b3/3b4 与 ARC-04.3c 的运行委托已由 EVO-03.2e/2f/2g 接入完整 interventional RED cohort；
EVO-03.3b 已冻结 candidate Request，EVO-03.3c2c2/3.4b 已完成连续 candidate cohort 与原生 H5c 比较。
Interventional Failure Attribution adapter、adversarial suite 与最终 Evaluation Receipt 仍未实现，因此
EVO-03 整体保持 partial。
