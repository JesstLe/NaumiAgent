# EVO-05.5f5p Durable Stable Observation Assessment

## 目标

将 [EVO-05.5f5o](EVO-05-5f5o-stable-observation-window.md) 的纯 Window 接入 current
[EVO-05.5f5n](EVO-05-5f5n-stable-runtime-exposure.md) Exposure Service、Evolution evidence DB 与
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) bounded paging，形成可持久化、并发幂等、可动态撤权的
`EvolutionRevalidationStableObservationWindowView`。

本切片只证明单 installation 的 current stable runtime liveness。它不聚合 Population，不消费 completed run，也不签发
stable stage completion、stable rollout 或 promotion authority。

## 启动授权与运行观察的时间边界

Stable Intent 的 `expires_at` 约束“是否还能启动新的 runtime”，而 stable observation 的最低持续时间可能长于 Intent TTL。
因此 5f5p 的前置修正确立两类独立 authority：

- `deployment_launch_input_current`：只允许在 Intent 未过期且完整 launch guardrail current 时启动新实例；
- `active_deployment_current`：exact Deployment Receipt、activation chain、active pointer、candidate slot 和 boot receipt 仍 current；
  它与 current heartbeat 一起维持已启动 runtime 的 observation authority。

Intent 过期不能授权新启动，但也不能让仍运行的 exact active Deployment 永远无法达到 stable duration。pointer rollback、slot/boot
漂移或 heartbeat 停止仍会撤销 observation authority。

## Store 与并发边界

`EvolutionRevalidationStableObservationWindowStore` 与 Stable Exposure Store 共用 Evolution evidence DB：

1. 持久化前重新反序列化 Window，并限制 JSON 为 8 MiB；
2. 以 stable `intent_id + subject_id` 作为 latest scope，不复用 percentage assignment key；
3. 从 Exposure Store 重读 exact Exposure，从 Harness Store 重读 Binding 与 exact sample slice；
4. 在 `BEGIN IMMEDIATE` 中再次逐字节验证 durable `exposure_json` 后 append-only 写入；
5. 相同 content identity 幂等返回，不同内容占用同一 identity 时失败关闭；
6. Store 不记录 API key、环境变量、argv、用户消息、模型输出或工具参数。

## 有界分页与竞态检查

Service 读取 Binding、heartbeat head 和 origin metadata，按
`max(chain_origin_sequence, head.sequence - 4999)` 选择 origin 或最近 5000 条 suffix，每页最多读取 500 条。分页完成后再次读取
heartbeat head 与 Binding，并要求 head 未变化、末 sample 逐字段匹配 heartbeat、Binding 未变化、cursor 单调前进。

空页、cursor 停滞、binding mismatch、head race、sample tamper 或来源缺失均失败关闭。suffix 的首条 previous digest 由 HAR cursor
读取路径锚定；纯 5f5o Window 只能验证局部链，只有本 Service 的 durable 重读才签发 current authority。

## 动态 View

`assess()` 只在 Exposure fact 与 active Deployment current 时形成新 Receipt；failed heartbeat 可以形成 durable breach，而不是因为
current runtime authority 已撤销就丢失故障证据。`inspect()` 每次重读 Exposure、Deployment、Binding 和当前 bounded ledger：

- passing 同时需要 latest Window、exact Exposure source、Exposure fact、active Deployment、Binding、ledger 与 current heartbeat；
- failed、gap 或 stale 在 durable dependencies 仍 current 时产生 pause/rollback input；
- 新 Window 会令旧 Receipt 标记 `newer_window_exists`，旧回执不能继续触发动作；
- pointer rollback 或 source drift 会撤销 passing 和 breach action authority；
- assessed_at 前进但 material evidence 不变时复用 Receipt，同时按新时钟重建 current assessment。

## 验收证据

- 真实 Stable Intent → Boot → Deployment → Runtime Exposure → heartbeat producer 全链运行至 stable minimum duration；
- 两个独立 Service 六路并发对同一 evidence 收敛为同一 Window identity；
- Intent 过期后 launch input 为 false，但 active Deployment 与 current runtime observation 仍有效；
- 无新 heartbeat 时动态 stale breach，failed terminal 可持久化为新 breach；
- 新 breach 撤销旧 passing Receipt 的 latest authority；pointer rollback 撤销所有 action authority；
- durable Window JSON 摘要篡改后读取失败关闭；
- 构造 5502 条连续 observation 后只读取最近 5000 条 verified suffix，仍可形成 passing Window；
- Ruff、compile、public lazy import 与本模块两个定向真实场景通过；未运行全量测试。

## 当前边界与下一步

5f5p 仍只证明一个 installation 的 liveness，`completed_runs_observed=0`，Population、completed-run、stable-stage、stable-rollout 和
promotion authority 固定为 false。

下一独立切片应建立 Stable Release-bound Execution Outcome：复用 [HAR-10.2k](../harness/HAR-10-2k-release-bound-chat-run-provenance.md)
将真实用户 run 绑定到 exact Stable Intent、Exposure 与 durable Window。它只形成单 run evidence，不能从单 Outcome 推断 stable rollout
完成。
