# EVO-06.1c1 H5c Quantitative Regression Opportunity Discovery

## 状态

Implemented。该切片同时交付 EVO-01.1c Eval Evidence adapter 和 EVO-06.1c 的第一类热点发现，
依赖 HAR-08.7e typed quantitative observations、HAR-08 H5a/H5c Store、EVO-01 Candidate Store 与
EVO-01.4b Composite Source Authority Router。

## 目标

把真实 H5c Comparison 中经 95% 置信区间确认的主定量指标回归转换为不可执行的 Evolution
Candidate。它覆盖 latency、USD cost、token consumption 及 count/ratio/scalar 等 runner 声明指标，
不从日志文本猜测数值，也不把一次运行的抖动、Suite 总耗时或 LLM 评价冒充回归证据。

## 权威输入和重算链

唯一入口是当前工作区内的 64 位 H5c Comparison ID。每次发现和每次 Review 前均执行完整重算：

1. 从 `HarnessStore` 按 workspace + ID 读取 immutable H5c receipt；
2. 按 receipt 的 suite/baseline batch 读取已晋升 Baseline，核对 ID、sample count 与 sample-set SHA；
3. 重读 Baseline/Current 两组完整 H5a cohort，要求 sample 数量与 receipt 精确一致；
4. 用 H5a Result 和原 `created_at` 调用 `build_eval_comparison_receipt()` 重建 H5c；
5. 要求重建对象、receipt SHA 与 durable projection 全部相等；
6. 调用既有 `compare_eval_repetitions()` 重算均值、均值差 95% CI 和方向结论；
7. 只接收 `primary=true`、带 case identity、带 target 且 CI 完全越过 0 的定量指标。

`direction=decrease` 只有 `confidence_low > 0` 才是回归；`direction=increase` 只有
`confidence_high < 0` 才是回归。`pass_rate` 与 Suite `duration_ms` 是统计层派生指标，不属于 runner
声明的 typed observation，本适配器不会用它们创建 Candidate。

## Evidence v2 与 Candidate 映射

`eval_metric_regression` 使用 `EvolutionEvidence schema_version=2`，新增冻结的
`EvolutionQuantitativeMetric`：metric、case fingerprint、unit、direction、target、baseline/current mean、delta 和
confidence interval。模型拒绝 NaN/Infinity、均值差不一致、delta 不在 CI、方向不构成回归及伪造来源字段。
旧来源继续使用 Evidence v1，不能携带定量字段。

| 输入 | Candidate 映射 |
|---|---|
| milliseconds | `eval_latency_regression` |
| usd | `eval_cost_regression` |
| tokens | `eval_token_regression` |
| count/ratio/scalar | `eval_metric_regression` |
| scope | suite/case/metric contract 的摘要，不含绝对路径 |
| root | baseline ID + typed metric contract + scope 的 canonical SHA-256 |
| Evidence ID | H5c receipt SHA + 数值统计 + root 的 canonical SHA-256 |
| expected metric | 原 metric name/direction/target，verifier=`harness_replay` |

同一 H5c 的并发重放由 Evidence identity 和 Candidate Store 事务收敛；同一 Baseline、同一 metric contract
的后续回归会聚合为同一 Candidate 的新 Evidence/revision。不同 Baseline 或不同指标合同不会误合并。
单个 Comparison 最多接收 128 个主指标回归；超限时整体拒绝适配，不静默截断。

Evidence 引用完整 H5c receipt SHA、Baseline sample-set SHA 和 Current sample-set SHA。Candidate 不保存
workspace 绝对路径、batch ID、Suite 路径、源码、日志、Prompt、用户对话或凭据。

## 动态 authority 与故障语义

`eval_metric_regression` 已加入唯一动态来源注册表。Engine 将其映射到
`EvolutionEvalMetricOpportunityService`，与 rollback/promoted Outcome reader 一起由 Composite Router
并发重验。以下任一情况都 fail closed：

- Comparison ID 非法、缺失、跨工作区或存储损坏；
- Baseline ID/digest/sample count 与 receipt 分离；
- 任一 H5a 样本缺失、追加、digest 或 Result 内容被篡改；
- H5c 无法原样重建，或统计 verdict 已不再是 `regressed`；
- 指标不是 primary、缺 target、CI 穿过 0，或只发生 pass-rate/派生 duration 变化；
- Candidate 中的 URI、Evidence ID、统计值、refs 或 root 与实时重算结果不完全相等。

来源失权不会删除历史 Candidate，但 `source_authority` Gate 立即 hard block，Proposal projection 不再产生。

## 双通道、权限和界面

- Agent Tool：`evolution_discover_eval_metric_opportunity(comparison_id=...)`
- Slash：`/evolution discover-metric <h5c-comparison-id>`
- Review filter：`/evolution list --source eval_metric_regression`
- New UI/TUI：沿用同一个 `evolution/review` typed event 与 Candidate Store projection
- 权限：normal mode 无二次确认、受每会话 50 次边界约束；bypass 直接允许；lockdown 阻断

Tool 与 Slash 共享同一 Service，不存在 CLI-only 适配器。回执显示原指标、均值变化、单位、delta 95% CI、
方向、target、Candidate ID/revision，并明确“不可执行、无实验/推广 authority”。

## 验收证据

- [x] 真实 SQLite H5a Baseline + Current cohorts 生成 H5c，并形成 latency Candidate。
- [x] 8 路并发发现同一 Comparison 收敛为单 Evidence、revision 1。
- [x] expected metric 保留 runner 的原 name/direction/target，而不是通用 rate 占位符。
- [x] H5a Result JSON 被篡改后，发现失败且既有 Candidate 动态 authority 失效。
- [x] improvement/unchanged/inconclusive 不形成回归 Candidate。
- [x] NaN/Infinity、CI 穿零和均值差伪造由 typed model 拒绝。
- [x] Agent Tool 与 Slash 走同一注册工具；New UI/TUI filter 接受新 source kind。
- [x] Permission、Engine composition、动态 router 和精确 Tool 注册表有聚焦回归测试。
- [x] 相关 Ruff、语法与小模块测试通过；未运行全量测试。

## 后续依赖

本切片只关闭“真实 Eval 成本/延迟/资源回归无法进入 Candidate”的断点，不代表 EVO-06.1c 或自进化闭环
完成。后续独立模块仍包括：缺失能力 Evidence、用户明确需求 Evidence、跨 Outcome 时间窗聚类、跨 Candidate
影响范围、EVO-01.5 可解释 Prioritization，以及 EVO-06.2 Capability Proposal。
