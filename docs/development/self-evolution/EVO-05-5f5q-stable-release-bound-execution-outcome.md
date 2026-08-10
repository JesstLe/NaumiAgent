# EVO-05.5f5q Stable Release-bound Execution Outcome

## 目标

将一个真实 stable 用户运行绑定到 exact Stable Intent、passing durable Window、managed release 与 Population member，形成
content-addressed `EvolutionRevalidationStableExecutionOutcome`，并由 append-only ledger 持久化及动态重验。

本切片消费 [EVO-05.5f5p](EVO-05-5f5p-durable-stable-observation-assessment.md)、
[HAR-10.2k](../harness/HAR-10-2k-release-bound-chat-run-provenance.md) `RunReleaseProvenance`、终态
`CompletionReceipt`、结构化 `RunUsage` 与覆盖完整运行区间的 HAR heartbeat chain。它只形成一个 run evidence，不聚合
`minimum_completed_runs`，不签发 stable-stage、stable-rollout 或 promotion authority。

## Exact lineage

`EvolutionRevalidationStableLivenessSourceRef` 冻结：

1. stable Intent、Window、Exposure、Deployment、Binding 的 ID 与 SHA-256；
2. candidate version、installation target 与固定 `stage=4/stable/100%`；
3. Population Snapshot ID/SHA/sequence/denominator 与 exact installation member ID；
4. ChatRun session/run/status、release provenance、Completion Receipt 与 RunUsage；
5. 从 run start 前 predecessor 到 receipt completion 后 successor 的连续 heartbeat slice；
6. execution start/completion、duration、result、recorded time 与 Outcome content identity。

Intent、Snapshot、member、Binding 或 run provenance 任一不一致都会失败关闭。调用方不能覆盖 success、Population input、stage、rollout
或 promotion authority。

## 结果与 authority

所有真实 terminal result（completed、partial、failed、cancelled）都形成 `stable_population_observation_input_authority`，因为错误和取消
也必须进入后续 stable guardrail。只有 `run_status=completed && receipt.outcome=completed` 才形成
`successful_completed_run_authority` 与 `stable_completed_run_authority`。

单 Outcome 的 `stable_stage_completion_authority`、`stable_rollout_authority` 和 `promotion_authority` 永远为 false。Runtime liveness
不能冒充用户任务成功；一个成功 run 也不能冒充 Population 或 rollout 成功。

## Heartbeat coverage

Service 使用共享的 release-bound coverage reader，最多有界读取 5000 条、每页最多 500 条，并在读取后复核 heartbeat head 与 Binding：

- predecessor observed time 不晚于 run start；
- successor observed time 不早于 receipt completion；
- slice sequence/previous SHA 连续、timeout 一致、gap 不超过 timeout；
- phase 仅允许 running/waiting；
- 每条 sample 绑定 exact Runtime Identity/Binding/instance/epoch/surface；
- receipt completion 尚未被 successor 覆盖时返回 coverage pending，不提前冻结 Outcome。

Store 和 inspect 会按首尾 sequence 从 HAR source 重读 exact slice。样本、Binding 或 head 漂移不会删除历史 JSON，但会动态撤销
Outcome authority。

## Durable ledger、并发与跨阶段去重

`EvolutionRevalidationStableExecutionOutcomeLedgerStore` 与 5f5p Window Store 共用 Evolution evidence DB：

1. Outcome JSON 限制为 8 MiB，写入前重新反序列化并重读 Window、ChatRun、Binding 与 coverage；
2. `BEGIN IMMEDIATE` 内再次读取 exact durable Window，再写入 stable outcome 表；
3. 同一 run 的完整 liveness source、provenance、receipt、usage 和 coverage 相同才允许幂等返回；
4. 同一 run 换 Window、Population/member 或其他 material evidence 会 identity conflict；
5. 若 run 已存在于 opt-in 或 percentage Outcome ledger，则拒绝 stable 重复计数；
6. 查询以参数化 SQL 和受限 ID/limit 执行，不保存 prompt、工具参数、工具输出或完整 assistant 正文。

`inspect()` 动态重读 durable Outcome、ChatRun、Window、Binding 和 coverage。任何来源漂移都撤销 execution、Population input 与
successful/stable completed-run authority；historical artifact 保留但不继续计数。

## 验收证据

- 真实 Stable Intent → Boot → Deployment → Runtime Exposure → durable Window → ChatRunStore 全链形成 completed Outcome；
- 六路并发 record 收敛到同一 content identity，durable lookup 与 Intent list 一致；
- Outcome 冻结 stage 4、100% exposure、Population Snapshot 与 installation member；
- failed run 仍形成 Population observation input，但不形成 successful/stable completed-run authority；
- completion 尚未被 heartbeat successor 覆盖时返回 pending；
- opt-in 与 percentage ledger 中已存在的 run 均被拒绝跨阶段重复使用；
- HAR sample、ChatRun usage source 和 durable Outcome digest 篡改后动态撤权或读取失败关闭；
- Ruff、compile、public lazy import 与 1 个真实端到端小模块测试通过；未运行全量测试。

## 当前边界与下一步

本切片只冻结单 run Outcome，尚未计算不同 run ID 的数量、失败率、完成率、延迟或成本，也不证明 current stable liveness。

下一独立切片应建立 `EVO-05.5f5r Stable Completed-run Aggregation`：动态消费 current 5f5p Window、不同 run ID 的 5f5q Outcome、
GREEN Baseline 与 stable stage guardrails，纳入失败/取消结果并达到 `minimum_completed_runs`。聚合结果仍不得直接冒充最终 promotion。
