# EVO-03.2b Metric Runner / Timeout Binding

## 目标

把 EVO-03.2a RED Baseline Cohort Request 中的抽象 metric verifier 绑定到仓库内真实、版本化、可机械
执行的 runner，并把 runner 最坏耗时纳入 Experiment 总预算。绑定产物仍不可执行：它只授予“哪个 runner
可以测什么”的 authority，不创建 worktree、不运行 baseline、不写 HAR-08 Result Store。

## 绑定产物

`EvolutionMetricRunnerBinding` 防篡改地引用：

- Baseline Cohort Request ID/digest；
- Validation Plan ID/digest；
- 每个 metric 的顺序、名称、方向、目标和 procedure digest；
- runner version、fixture kind/digest、单样本 timeout；
- Profile checks 与 metric runners 的 cohort 总耗时；
- blocked reason、预算 headroom 与固定的 `execution_ready=false`。

Builder 会重新解析 Request 与 Plan，并逐项比较 Candidate、baseline、metric 和 procedure authority。嵌套
对象通过 `model_copy/model_construct` 绕过校验后也不能进入绑定产物。

## Runner Registry

### `self_review_static`

- 真实 runner identity：`self_review_static@1`；
- 只接受 `self_review.<SelfReviewFindingCode>.count`；
- direction 必须为 decrease，target 必须是非负整数，否则以
  `self_review_metric_contract_invalid` 阻断；
- fixture 是排序后的 Validation Plan paths 与 finding code 的摘要；
- 单样本 timeout 固定 30 秒；
- 无模型、无网络、无写副作用；
- 实际扫描复用 `scan_self_review_files()` AST scanner，未知 finding code fail-closed。

### `harness_replay`

- 绑定现有 HAR-05 `safe_replay@1`，不创建第二套 replay runner；
- 当前 Baseline Request 尚未携带精确 Harness replay run/baseline lookup；
- 因此返回 `replay_fixture_required`，而不是假定任意历史 run 可比较；
- 后续审计进一步确认 Safe Replay 不执行 baseline/candidate 源码，主指标 `replay_reproduced` 与
  `harness.<failure>.rate` 不同；即使补齐 run-id fixture 也不能把它升级为干预型代码验证。

### `feedback_recurrence`

- 当前没有可信 observation-window runner；
- 返回 `feedback_window_runner_unavailable`；
- 用户沉默、没有新反馈或单次对话结束都不能被编码为“复发率为 0”。

## 超时预算

绑定器计算：

`required = profile_check_timeout_per_sample × samples + Σ(metric_timeout × samples)`

若一个本来可执行的 metric 使 `required` 超过 Contract `max_duration_seconds`，该 entry 变为
`metric_duration_budget_exceeded`。当前真实 5-sample fixture 的 Profile checks 已占满 1200 秒，因此任何
额外 metric runner 都不能冒充 execution-ready；后续需要缩短可信 Profile timeout 或显式扩大 Contract 预算。

Blocked runner 没有 timeout 时不会伪造耗时，其缺失由 blocking code 表达。

## 安全与 UX 语义

- `binding_status=ready` 只表示所有 metric authority 与 timeout 完整，不表示 cohort 可以执行；
- `execution_ready` 始终为 false，ARC-04 worker 仍是强制依赖；
- 绑定不保存绝对 workspace/worktree 路径、源码、argv 或 secret；
- blocking codes 稳定排序并纳入 artifact digest，UI 后续可按原因展示“缺 fixture / 缺 runner / 超预算”；
- 不调用模型，不访问网络，不修改主工作树或 Lease worktree。

## 验收证据

- 对真实 Python 文件运行 AST scanner，`broad_except` finding 与绑定 finding code 一致；
- `self_review_static@1` 只对已知 finding code ready，未知 metric blocked；
- Replay 绑定真实 `safe_replay@1`，缺精确 fixture 时 blocked；
- Feedback recurrence 无 runner 时 blocked，不产生虚构数值；
- 真实 Mutation Receipt→Plan→Profile Binding→Baseline Request→Metric Binding 确定性一致；
- 当前 direct-feedback fixture 显示 1200 秒 Profile 预算、0 秒已绑定 metric 预算和明确 blocked 状态；
- nested blocking code 篡改触发 digest 校验失败；
- Engine 组合 Builder，构建前后主工作树和 Lease worktree 不变。

## 当前不足与下一步

- HAR-08.7e 已补齐数值 observation、方向比较与重复样本统计，为 `self_review.*.count` 提供真实载体；
- `harness_replay` 保持 blocked；需要运行项目代码的 Harness failure 指标必须改用 ARC-04 interventional
  runner，不能仅补 Replay fixture；
- `feedback_recurrence` 需要独立 observation-window 数据模型、最短观察期和缺失数据语义；
- EVO-03.2c/3.3a 已完成 Self-Review 静态 RED/GREEN cohort、H5a 连续 sample 与 completion receipt；
- ARC-04 ephemeral worker 和执行项目代码的 baseline 仍未实现；
- EVO-03.4a/3.5a 已冻结 RED→GREEN 数值判定与 Failure Attribution；下一步跨查 ARC-04/EVO-03.6。
