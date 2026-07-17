# HAR-08.7d 重复样本与统计 Comparator

## 1. 目标

在 HAR-08.7a Identity gate、8.7b 机械比较与 8.7c Policy gate 之后，为重复运行提供可解释的
均值、离散度、95% 置信区间和逐 case 波动证据。统计层不能把评测基础设施错误、样本不足或
Identity 不一致包装成改善，也不能用总体均值掩盖同一 case 的不稳定。

本切片只实现纯比较核心与有界文本 renderer；不提前实现 Result 持久化、Baseline promote、CLI/API
入口或自进化采纳决策。

静态 Runner 增加 `evaluate_suite_repetitions()`：在同一源码 fingerprint 边界内真实运行 5..100 次，
运行完成后统一复核工作树，并把计划重复次数写入每个 Result 的 Identity。它不调用模型、不执行命令，
也不把循环次数伪造成若干独立单次 Baseline。整组默认有 60 秒、可验证范围 1..600 秒的总预算；
预算耗尽返回 typed partial batch、完成数和稳定原因码，不能伪装为完整样本组。

## 2. 输入契约与 Gate 顺序

`compare_eval_repetitions(baseline_runs, current_runs)` 接收两组 typed
`HarnessEvalSuiteResult`，按以下顺序拒绝不可信输入：

1. 每组默认至少 5 次、最多 10000 次；安全上限和最小值不可由坏参数绕过。
2. 每个结果必须含可验证 Identity，且 Identity 的 `configuration.repetitions` 必须等于实际组内
   样本数。
3. 以 Baseline 首样本为结构锚点，复用 8.7b 比较器检查所有样本的 Identity、Suite/Policy digest、
   case 集、runner 和汇总状态。
4. 任一 evaluation error、skip、重复 case 或结构漂移立即返回 inconclusive；Identity 冲突返回
   incompatible。

只有全部 gate 通过后才计算统计量。

## 3. 统计量

每组分别计算：

- `pass_rate`：每次 Suite 的通过 case 比例；
- `duration_ms`：每次 Suite 的真实总耗时；
- 样本数、算术均值、样本标准差；
- 双侧 95% Student-t 均值置信区间。

Current-Baseline 的均值差使用 Welch 标准误与 Welch-Satterthwaite 自由度，不假设两组方差相等。
小样本 t 临界值采用保守分桶表；大样本收敛到 1.96。通过率区间限制在 0..1，差值区间保留符号。

## 4. 波动与 Verdict

逐 cohort、逐 case 汇总重复运行中的状态集合：

- 同一 case 只有一种状态：稳定；
- 同一 case 同时出现 passed 与 implementation failure：`flaky`，列出 case、cohort 和实际状态；
- evaluation error/skip 在更早的可信度 gate 返回 inconclusive，不混入 flaky。

无 flaky 时，以通过率均值差的 95% CI 判定：

- CI 全部大于 0：improved；
- CI 全部小于 0：regressed；
- 差值为零：unchanged；
- 差值非零但 CI 跨零：inconclusive，不声称“无变化”。

耗时统计作为独立证据展示，不在本切片中改变产品正确性 verdict；后续 Policy 可声明明确的延迟
guardrail。

## 5. 已验证场景

- 两组各 5 次全绿结果形成稳定均值、标准差与零差值 CI；
- 稳定 100% → 50% 通过率形成统计回归；
- 仅一次 case 摇摆即标为 flaky，即使总体均值仍较高；
- 4 次样本返回 sample insufficient；Identity repetitions 与实际数量不一致返回 incompatible；
- evaluation error 返回 inconclusive，renderer 明确显示评测错误；
- 非法 minimum sample 参数立即拒绝。
- 真实临时 Git 仓库中的 production hello Suite 连续运行 5 次，五个 Result 均绑定 repetitions=5 的
  同一 Identity 配置，并可形成 unchanged 统计结论。
- 总预算在第一轮后耗尽时返回 `partial/repetition_budget_exhausted`，保留已完成结果但不形成完整比较。

## 6. 后续依赖

- HAR-08 H5：原样保存每次 typed Result、样本组 identity 和统计 Comparison；
- HAR-08.8：向 Slash/Tool/API/New UI 暴露比较详情与 Baseline promote gate；
- HAR-09.6：Proposal 实施后引用 before/after 统计比较；
- EVO-03.4：自进化补丁只有在主指标改善、guardrail 无退化且无 flaky 时才能进入反思决策。
