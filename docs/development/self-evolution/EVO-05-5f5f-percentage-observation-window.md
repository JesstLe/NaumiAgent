# EVO-05.5f5f Percentage Runtime Observation Window

## 目标

消费 exact [EVO-05.5f5e](EVO-05-5f5e-percentage-runtime-exposure.md) Exposure Receipt、同一 managed terminal
runtime 的 [HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) append-only sample chain，以及不可变
percentage rollout stage/assignment guardrail，形成 content-addressed
`EvolutionRevalidationPercentageObservationWindow`。

本切片只回答“这个被选中的 installation 是否持续运行了足够长时间且心跳连续”。它不把单 installation liveness
冒充 cohort health、completed run、percentage stage completion、stable rollout 或 promotion。

## 输入与不可伪造边界

构建器只接受 5f5e Receipt、完整有序 sample tuple 与带 offset 的 `assessed_at`：

1. Exposure 必须已具有 installation exposure 与 percentage observation input authority；
2. Plan 的第三阶段必须是 `percentage/limited`，Assignment 必须仍精确绑定同一 Plan、exposure percent、population
   denominator、target member count 与 member rank；
3. sample 1/2 必须逐对象等于 Exposure 冻结的 startup/ready pair；
4. 每个 sample 必须绑定同一 canonical workspace、Binding、Runtime Identity、surface、subject、instance、epoch 与 timeout；
5. 只接受 startup origin、从 sequence 1 开始的连续 sequence/SHA-256 链，拒绝 legacy snapshot、缺页、拼接与时钟倒退；
6. 输入最多 5000 个样本，策略所需样本超过该界限时失败关闭。

调用方不能覆盖 observation duration、sample count、最大 gap、cohort 参数、状态或任何 authority。所有投影均由冻结
artifact 与 Harness evidence 机械重算，Window ID/SHA-256 覆盖完整内容。

## 状态算法

`minimum_sample_count = ceil(minimum_observation_seconds / timeout_seconds) + 1`。观察时长只计算最后一段连续
`running/waiting` suffix，draining/stopped/failed 后重新运行不会继承旧时长。

- `breached`：任一样本为 failed、相邻样本间隔大于 timeout，或末样本在 assessed_at 已 stale；
- `insufficient`：末状态不再 active、连续 operational 样本不足，或连续 observation duration 不足；
- `passing`：无 breach 且所有 duration/sample/liveness guardrail 同时满足。

只有 `passing` 签发 `percentage_runtime_window_authority`。只有 `breached` 产生 pause/rollback input authority。
`completed_runs_observed` 固定为 0，其余 cohort、stage completion、stable 与 promotion authority 永远为 false。

## 验收结果

- 使用真实 5f5a→5f5e artifact、ReleaseSlot、TerminalRuntimeLifecycle 与 Harness SQLite 形成 exact exposure；
- 从 startup/ready 开始追加 480 个真实 producer pulse，机械证明 4 小时等价 percentage 窗口；
- 初始短链为 insufficient，完整链为 passing，timeout 后为 breached；
- 删除中间 sample 会因 sequence/hash chain 断裂失败关闭；
- 正常 stopped 清空连续 operational suffix，不能复用历史 passing；
- 其他合法 runtime 的 startup pair 不能替代 exact exposure origin；failed terminal 产生 rollback/pause input；
- content identity 重验会拒绝手工提升 completed-run authority；
- 本模块 ruff 与 2 个真实小场景测试通过；未运行全量测试。

## 当前边界与下一步

本构建器不读取 Store，因此传入旧 Exposure 或旧 sample tuple 时不会自行发现 pointer/Deployment 漂移，也不负责跨 500 条分页。
下一最小切片为 `EVO-05.5f5g Durable Percentage Observation Assessment`：以 bounded HAR paging 重建并持久化 Window，
在 inspect 时重验 current Exposure/Deployment/Binding 与新增 terminal sample，从而动态撤销旧 passing authority。

群体 completed-run outcome、percentage cohort aggregation 与 stage completion 必须在 5f5g 之后另建 evidence，不能合并进本切片。
