# EVO-05.5f5h Percentage Release-bound Execution Outcome

## 目标

将真实 percentage 用户运行绑定到 exact Assignment、Exposure、passing Window 与 managed release，形成独立、不可变、
可动态重验的 `EvolutionRevalidationPercentageExecutionOutcome`。本切片消费
[EVO-05.5f5g](EVO-05-5f5g-durable-percentage-observation-assessment.md)、
[HAR-10.2k](../harness/HAR-10-2k-release-bound-chat-run-provenance.md) 的 `RunReleaseProvenance`、终态
`CompletionReceipt`、结构化 `RunUsage` 和覆盖完整运行区间的
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) heartbeat chain。

本 Outcome 只证明一个真实 run 的机械终态。它不把 runtime liveness 冒充用户任务成功，也不签发 percentage stage
completion、stable rollout 或 promotion authority。

## 权威边界

每个 Outcome 冻结以下 exact source：

1. `assignment/window/exposure/deployment/binding` 的 ID 与 SHA-256；
2. candidate version、installation target、percentage stage order、exposure percent、population denominator 与 member rank；
3. `ChatRunRecord` 的 session/run/status、release provenance、终态 Completion Receipt 与 Run Usage；
4. 从 run 开始前 predecessor 到 receipt 完成后 successor 的连续 HAR observation slice；
5. execution start/completion、duration、result 与 Outcome content identity。

所有真实 terminal result（completed、partial、failed、cancelled）都具有 cohort observation input authority，因为失败样本也必须
计入后续 guardrail；只有 `status=completed && receipt.outcome=completed` 才具有 successful/percentage completed-run
authority。单 Outcome 的 stage-completion、rollout、stable 与 promotion authority 永远为 false。

## 隐私、用量与输入约束

Outcome 不复制用户 prompt、工具参数、工具输出或完整 assistant 正文。它保留公共 Completion Receipt 中最多 2000 字符的有界
summary，以及已有数量上限的 changes、validations、risks、approvals 和 evidence refs；这些字段仍须遵循回执层的脱敏规则。
`RunUsage.reported_cost_usd` 只是 provider 报告的运行用量证据，不是账单或支付 authority。

持久化入口限制 Outcome JSON 为 8 MiB；assignment ID 使用固定前缀与 24 位十六进制格式；list limit 只能为 1..100；heartbeat
样本只能为 2..5000。SQLite 查询全部参数化，写入使用 `BEGIN IMMEDIATE`。

## 有界覆盖读取与竞态保护

Service 读取当前 heartbeat head 与 exact Binding，再从 HAR Ledger 最多读取最近 5000 条样本，每页最多 500 条。分页完成后
重新读取 heartbeat head 与 Binding，并要求：

- head 未改变且逐字段匹配最后一个 sample；
- 每页属于 exact binding，cursor 必须单调前进；
- predecessor 的 observed time 不晚于 run start；
- successor 的 observed time 不早于 receipt completion；
- slice 内 sequence 连续、previous SHA-256 连续、timeout 一致且 gap 不超过 timeout；
- phase 仅允许 running/waiting，且 sample 是 observation fact，而不是 current liveness authority。

缺少 predecessor/successor 时返回 coverage pending，不提前冻结 Outcome。Store/inspect 会按首尾 sequence 从 HAR source 重读 exact
slice；ledger、binding 或 ChatRun 任一来源变化都会动态撤销 authority。

## Store、并发与去重

`EvolutionRevalidationPercentageExecutionOutcomeLedgerStore` 与 5f5g Window Store 共用 Evolution evidence DB：

1. 写前重新反序列化 artifact，并重读 Window、ChatRun、Binding 与 coverage；
2. 同一 `run_id` 只能对应一个 material evidence；六路并发写入相同 evidence 幂等返回同一 Outcome；
3. 若同一 run 已进入 Opt-in Outcome Ledger，则拒绝跨阶段重复计数；
4. historical Outcome 保留为历史事实，但 `inspect()` 每次动态重验 current source；
5. 新 Window 或后续 liveness 变化不会改写历史 Outcome；下一层聚合必须独立要求 current percentage liveness。

## 验收结果

- 真实 5f5a→5f5g、TerminalRuntimeLifecycle、Harness SQLite 与 ChatRunStore 链形成 completed Outcome；
- 六个并发 record 调用得到同一 content identity，durable lookup 与 assignment list 一致；
- failed terminal run 仍形成 cohort observation input，但不获得 successful completed-run authority；
- receipt completion 尚未被 heartbeat successor 覆盖时失败关闭；
- 同一 run 已存在于 Opt-in Ledger 时拒绝 percentage 重复计数；
- HAR sample、ChatRun usage source 或 durable Outcome digest 被篡改后动态撤权或读取失败关闭；
- 新生产模块、单个真实场景测试、ruff、compile/public import、YAML 与 diff check 通过；未运行全量测试。

## 当前边界与下一步

本切片只冻结单 run outcome，尚未把同一 Assignment 下多个不同 run 聚合为 cohort evidence。下一最小切片应为
`EVO-05.5f5i Percentage Completed-run Aggregation`：动态消费 current passing Window、current Outcome、GREEN Baseline 与 percentage
guardrails，按不同 run ID 去重并计入失败结果；达到 `minimum_completed_runs` 之前不得形成 Stage Completion Evidence。
