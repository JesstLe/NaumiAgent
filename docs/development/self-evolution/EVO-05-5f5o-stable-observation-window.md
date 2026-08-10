# EVO-05.5f5o Stable Runtime Observation Window

## 目标

消费 exact [EVO-05.5f5n](EVO-05-5f5n-stable-runtime-exposure.md) Exposure Receipt、同一 managed terminal
runtime 的 [HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) append-only sample chain，以及 Rollout
Plan 第四阶段的 stable guardrail，形成 content-addressed `EvolutionRevalidationStableObservationWindow`。

本切片只回答“这一台 100% stable population 成员是否持续运行足够长且心跳连续”。它不把单 installation liveness 冒充
population health、completed run、stable stage completion、rollout completion 或 promotion。

## 输入与 lineage

构建器只接受 5f5n Receipt、完整有序 sample tuple 和带 offset 的 `assessed_at`：

1. Exposure 必须记录 stable installation exposure，并保留下游 observation input 标记；
2. Plan 第四阶段必须为 `stable/stable/100%`，Intent 与 Installation Proof 必须 exact 绑定同一 Plan；
3. Population Snapshot ID/SHA/sequence/denominator 与 installation member 必须在 Intent、Proof 和 Credential 间一致；
4. `origin` scope 的前两条 sample 必须逐对象等于 Exposure startup/ready pair；
5. `suffix` scope 必须来自 startup origin，位于 ready 后并保留首样本 previous digest；
6. 所有 sample 必须绑定同一 workspace、Binding、Runtime Identity、surface、subject、instance、epoch 和 timeout；
7. sequence/SHA-256 chain 必须连续，拒绝缺页、拼接、legacy origin 和时钟倒退；
8. 输入最多 5000 条；策略所需样本超过界限时失败关闭。

调用方不能覆盖 duration、sample count、gap、Population 字段、状态或 authority。Window validator 会从 nested artifacts 和 samples
完整重算所有字段及 content identity。

## 状态算法

`minimum_sample_count = ceil(stable.minimum_observation_seconds / timeout_seconds) + 1`。观察时长只计算最后一段连续
`running/waiting` suffix；stopped、draining 或 failed 会清空先前连续时长。

- `breached`：出现 failed、相邻 sample gap 大于 timeout，或末样本在 assessed_at 已 stale；
- `insufficient`：末状态不 active、连续 operational sample 不足或 duration 不足；
- `passing`：无 breach 且 duration/sample/liveness 同时达到 stable stage 门槛。

只有 passing 产生 `stable_runtime_window_authority`；只有 breached 产生 pause/rollback input。`completed_runs_observed=0`，
population、completed-run、stable-stage、stable-rollout 与 promotion authority 永远为 false。

## 验收证据

- 真实 Stable Intent → Boot → Activation → Runtime Exposure 与 TerminalRuntimeLifecycle/Harness SQLite 全链运行；
- 初始 startup pair 为 insufficient，追加真实 producer pulse 到 stable stage 门槛后为 passing；
- timeout 后为 breached，生成 pause/rollback input；
- 删除中间 sample 因 sequence/hash chain 断裂失败；
- stopped 清空 continuous operational suffix，不能复用历史 passing；
- 其他合法 runtime 的 startup pair 不能替代 exact Exposure；failed runtime 形成 breach；
- 手工提升 completed-run authority 被 content validator 拒绝；
- Ruff、compile、public lazy import 与本模块 2 个真实场景通过；未运行全量测试。

## 当前边界与下一步

本切片是纯 artifact builder，不读取 durable Store。调用者传入旧 Exposure 或截断 suffix 时，构建器只能验证局部 artifact/chain，
不能证明它们仍是当前 authoritative head。

[EVO-05.5f5p](EVO-05-5f5p-durable-stable-observation-assessment.md) 已建立 Durable Stable Observation Assessment：Service 从
HAR origin/suffix 有界分页重建并持久化 Window，inspect 时重验 Exposure、active Deployment、Binding、当前 ledger head、pointer 与
新增 failure/stale。只有该动态 assessment 才可作为后续 stable release-bound run evidence 的 liveness 前置。
