# HAR-08.7e Quantitative Metric Observations

## 目标

让 HAR-08 的 typed Result、机械 Comparator、Policy、重复样本统计和 H5a Store 能承载真实数值指标，
而不再把所有评测压缩成 case 通过率与 suite 耗时。这是 `self_review.*.count`、权限阻断率、token、成本、
延迟等指标进入 RED/GREEN 对照的共同数据前置。

本切片只扩展指标证据与比较合同，不执行 Self-Review、Tool、Check 或模型，不开放 Sandbox/Live runner。

## Typed Observation

`HarnessEvalMetricObservation` 固定：

- `metric`：稳定、无控制字符的机械指标名；
- `value`：范围在 ±10^15 内的有限数值，拒绝 bool、NaN/Infinity 与失控极值；
- `unit`：`count | ratio | milliseconds | tokens | usd | scalar`；
- `direction`：`decrease | increase`；
- `target`：与 value 同单位的有限目标；
- `primary`：一个 case 恰好有一个主指标。

单位约束：

- count/tokens 必须是非负整数；
- ratio 的 value/target 必须位于 0..1；
- milliseconds/usd 不得为负；
- scalar 允许有符号有限值。

Case observations 必须按 metric 排序且不得重复。只要携带 observation，`primary_metric` 必须精确引用
唯一 primary observation，并且 `passed/implementation_failure` 必须与方向和 target 的机械判定一致。
`evaluation_error/skipped` 不得夹带部分数值，避免失败 runner 留下可被误用的指标。

既有 Protocol/Replay Result 没有 observations 时保持兼容，原有 `primary_metric` 仍可使用。

## 机械比较

Identity、case set、runner 和 run status gate 通过后，Comparator 还会检查两侧 observation：

- metric 集合必须相同；
- unit、direction、target、primary 角色必须相同；
- 合同漂移返回 `inconclusive`，不产生产品回归结论；
- 每个 observation 生成 baseline/current/delta/relative delta；
- 输出 key 使用 `case:<case-id>:<metric>`，避免多 case 同名指标碰撞。

主指标在 case status 未跨越 target 时仍参与方向判断。例如 count 从 5 降到 2，即使目标 0 尚未达成，
也形成 quantitative improvement；从 5 升到 7 形成 regression。若 status 已跨越 target，则由既有 case
transition 计数，数值信号不重复计数。

`max_regressions` Policy 同时计算 case regression 与未跨 target 的 quantitative regression，因此
“仍然及格但指标变差”不会绕过零回归门。

## 重复样本统计

HAR-08.7d 在既有 pass rate/duration 之外，对每个 case observation 计算：

- 样本数、均值、样本标准差；
- Student-t 95% CI；
- baseline/current 均值差及其 CI；
- unit、direction、target、case 和 primary 元数据。

方向判定使用完整置信区间：decrease 指标的差值上界小于 0 才算改善；increase 指标相反。任一主指标
显著回归优先判定 regression；无回归但 CI 跨零时保持 inconclusive；只有方向证据充分时才报告 improvement。
duration 继续作为诊断指标，不单独决定产品 verdict。

## Store 与完整性

- H5a 使用原有 immutable workspace/batch/suite/sample key 保存 observations；
- Result digest、cohort digest 与 H5c receipt 会自然覆盖 observation 全字段；
- canonical payload 只排除 volatile duration，不排除数值证据；
- observation 不保存源码、argv、绝对路径、模型输出或 secret。

## 验收证据

- NaN、非整数 count、重复 observation、伪造 passed status 全部被 schema 拒绝；
- count 5→2 在目标仍为 0 时报告机械改善，5→7 报告机械回归；
- target/unit/direction 漂移返回 inconclusive 且无 metric delta；
- 目标内数值回归触发默认 `max_regressions=0` Policy；
- 5 个 baseline/current 样本生成 direction-aware 95% CI；
- observation 经 H5a 写入、读取后字段与摘要保持一致；
- 5×RED count=3 与 5×GREEN count=0 生成 H5c statistical improvement 与 passed receipt；
- 原有 Protocol、Replay、Policy、Suite Comparator 与 Statistics 聚焦回归继续通过。

## 对 Self-Evolution 的影响与下一步

审计确认 `safe_replay@1` 是非干预型 runner：它只证明持久证据可复现，主指标是
`replay_reproduced`，不会在 baseline/candidate 源码上重新执行，因此不能直接证明
`harness.<failure>.rate` 改善。

EVO-03 下一切片改为 **EVO-03.2c Self-Review Static RED Baseline**：从精确 Git baseline 读取
Validation Plan 文件，执行 `self_review_static@1`，将 finding count 写入本切片定义的 observation，再以
连续 sample index 进入 H5a。该路径无模型、无网络、不执行项目代码，可先于 ARC-04 Sandbox 完成。

需要执行 Profile checks、Tool 或项目代码的 harness failure 指标仍必须等待 ARC-02/ARC-04，并应采用新的
interventional runner；不得把 Safe Replay 的可重复性当作代码修复效果。
