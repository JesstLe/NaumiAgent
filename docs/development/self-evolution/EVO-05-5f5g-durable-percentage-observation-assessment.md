# EVO-05.5f5g Durable Percentage Observation Assessment

## 目标

将 [EVO-05.5f5f](EVO-05-5f5f-percentage-observation-window.md) 的纯 Window 构建器接入 current
[EVO-05.5f5e](EVO-05-5f5e-percentage-runtime-exposure.md) Exposure Service、Evolution evidence DB 与
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) bounded paging，形成可持久化、可并发幂等、可在
inspect 时动态撤权的 `EvolutionRevalidationPercentageObservationWindowView`。

本切片只证明单 installation 的 current runtime liveness window。它不聚合 cohort，不生成 completed-run evidence，也不签发
percentage stage completion、stable rollout 或 promotion authority。

## 长生命周期前置修正

5f5f 最初要求每个 Window 都携带 sequence 1/2 和从起点开始的完整链；这与 5000 样本上限冲突，runtime 超过 5000 个
heartbeat 后将永久无法评估。5f5g 将契约收敛为两种明确 scope：

- `origin`：samples 以 exact Exposure startup/ready pair 开始；
- `suffix`：samples 是 Harness Store 从受验证 cursor 读取的最近最多 5000 条连续链，首样本保存
  `suffix_anchor_sha256`，仍绑定 startup origin、exact Binding/Runtime Identity 与 Exposure。

纯构建器可以验证 suffix 的局部 sequence/hash continuity；只有 Store/Service 从 HAR cursor 重新读取并逐对象比对后，View
才会签发 durable current authority。这样既保持输入上限，也不会在长生命周期中静默丢失评估能力。

## Store 与并发边界

`EvolutionRevalidationPercentageObservationWindowStore` 与 Exposure Store 共用 Evolution evidence DB：

1. 写入前重新反序列化 Window，并限制 JSON 为 8 MiB；
2. 从 Exposure Store 重读 exact Assignment Exposure；
3. 从 Harness Store 重读 exact Binding 与 Window sample slice；suffix 使用 `first_sequence - 1` 的已验证 cursor；
4. 在 `BEGIN IMMEDIATE` 内用参数化 SQL 重读 exact `exposure_json`，再写 append-only Window；
5. Window ID 相同且内容相同幂等返回，identity 相同而内容不同失败关闭；
6. latest key 固定为 `(assignment_id, subject_id, assessed_at, window_id)`，调用方不能覆盖状态或 authority。

Store 不记录 API key、环境变量、argv、用户消息、模型输出或工具参数。

## 有界分页与竞态检查

Service 先读取 Binding 和 heartbeat head，再读取 origin metadata，计算：

`first_sequence = max(chain_origin_sequence, head.sequence - 4999)`

之后每页最多 500 条读取到原 head。分页完成后再次读取 heartbeat head 与 Binding，并要求：

- head 未变化且逐字段等于末 sample；
- Binding 在整个分页期间未变化；
- 每页 binding ID/SHA-256 一致，cursor 单调前进；
- suffix 首样本的 previous digest 由 HAR cursor 读取路径验证。

任何 head/binding race、空页、cursor 停滞、ledger 篡改或来源缺失均失败关闭，不以部分页签发 authority。

## 动态 View 权限

`inspect()` 不信任旧 Receipt 的状态，而是重读 Exposure、Deployment、Binding 与当前 bounded ledger 并重建 assessment：

- passing 需要 Window/latest/source、Exposure fact、Deployment launch input、current runtime exposure、Binding 与 ledger 全部 current；
- failed、gap 或 stale 在 Deployment/来源仍 current 时形成 breached，并签发 pause/rollback input；
- pointer/slot/boot/Exposure durable source 漂移会撤销所有 Window 与 pause/rollback authority；
- 新 Window 写入后旧 Receipt 标记 `newer_window_exists`，不能继续触发动作；
- stopped 为 insufficient，不继承旧 passing suffix。

## 验收结果

- 真实 5f5a→5f5e、ReleaseSlot、TerminalRuntimeLifecycle 与 Harness SQLite 链形成 4 小时等价 passing Window；
- 两个独立 Service 并发评估同一 material evidence，得到同一 content identity 与 durable Receipt；
- assessed_at 前进但 material evidence 不变时复用 Receipt，同时动态 current assessment 使用新时钟；
- 无新 heartbeat 超时后动态变为 stale breach；failed terminal 动态 breach 并可持久化为新 Window；
- 新 breach 写入后旧 Window 失去 latest authority；pointer rollback 撤销 launch input，旧 breach 不再产生回滚输入；
- 构造 5502 条连续 observation 后只读取最近 5000 条 verified suffix，仍可机械证明 passing；
- durable Window JSON 摘要篡改后读取失败关闭；
- 本模块 2 个真实小场景、5f5f 回归、ruff、compile、public import、YAML 与 diff check 通过；未运行全量测试。

## 当前边界与下一步

5f5g 只观察单 selected installation，`completed_runs_observed` 仍为 0。下一最小切片应为
`EVO-05.5f5h Percentage Release-bound Execution Outcome`：复用 HAR-10.2k run provenance 与 5f4c outcome 约束，把真实
percentage 用户 run 绑定到 exact Assignment/Exposure/Window。随后才能设计 cohort completed-run aggregation 与 percentage
stage completion，不能直接从 liveness 推断 rollout 成功。
