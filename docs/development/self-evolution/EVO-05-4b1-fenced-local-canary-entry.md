# EVO-05.4b1 Fenced Local-Canary Stage Entry

## 目标

在 EVO-05.4a immutable Plan 与真正的 canary executor 之间增加一层短期、可撤销、可动态 fencing 的执行权威。
本切片允许后续执行器物化 immutable GREEN、运行 local canary、记录 observation；不执行这些操作本身，也不开放 opt-in、percentage、stable、
Git、merge、push 或 publish。

## Stage-entry authority

`EvolutionRevalidationRolloutStageEntryService.issue()` 每次动态检查 Plan 背后的 Fresh Decision 仍 current approved，只允许 Plan 第一个
`local_canary` stage。Receipt 绑定：

- exact Plan/Decision/Contract/stage digest；
- append-only attempt 与 previous receipt hash；
- 当前 kill-switch generation/event digest；
- `materialize_immutable_green`、`run_local_canary`、`record_canary_observation` 三项精确操作；
- read-only workspace、必须使用 ephemeral root、network deny、process-tree cancel；
- Plan 冻结的 minimum completed runs，以及 observation window 派生的短期 expiry；
- Control Plane HMAC attestation。

Receipt 只设置 `local_canary_execution_authority=true`。所有后续阶段和 Git/发布权限均为 false。

## Emergency kill switch

Control Store 以 workspace-scoped append-only hash chain 保存 `pause/resume` event，每个 event 都由 Control Plane HMAC 签署。
没有事件时为 generation 0 active；任一 pause 或 resume 都推进 generation。Stage entry 必须绑定 exact generation：

- pause 写入后，所有旧 entry 无需修改即可动态 `fenced`；
- paused 状态禁止签发新 entry；
- runtime/monitor 可以 fail-safe pause，但只有 user/operator control-plane actor 可以 resume；
- resume 不复活旧 entry，只允许生成链接旧 receipt 的新 attempt；
- Decision/专业签名失效优先显示 `plan_stale`；
- expiry 到达时 entry 机械失权。

Store 在 `BEGIN IMMEDIATE` 中重读 Plan、最新 control event 与 entry chain head，避免 kill switch 与签发之间的竞态。8 个独立 Service
并发签发会复用首次 durable entry，而不是形成多份执行权威。

## 验收结果

- approved Plan 只签发 local-canary scope，workspace/network/Git/publish 权限边界正确；
- 8 路不同微秒时钟并发只形成一份 attempt 1；
- operator pause 立即 fence attempt 1 并阻断新签发；
- resume 后生成 hash-linked attempt 2，旧 attempt 保持 fenced；
- 专业 Principal 换钥后 attempt 2 动态变为 `plan_stale`；
- expiry 到达后失去 canary execution authority，且不能获得后续 stage 权限；
- 2 个真实 Approval/Plan/SQLite/HMAC 场景通过；未运行全量测试。

## 当前不足与下一切片

本切片是执行前权威，不是执行日志，也没有运行 canary。EVO-05.4b2 必须：

1. 从 immutable GREEN source 在受管 ephemeral root 真实物化候选；
2. 使用本 entry 的 operation/network/workspace/time/run scope 执行 local workload；
3. 将 admission→materialized→running→terminal 状态写入 crash-safe journal；
4. 每次状态推进前动态读取 kill switch 和 Plan；
5. 中断后通过 content digest 恢复或清理，不把启动回执当成完成回执。

直到 EVO-05.5 monitor、EVO-05.6 automatic rollback 和 EVO-05.7 Outcome 回注完成，仍不得宣称自进化真实闭环完成。
