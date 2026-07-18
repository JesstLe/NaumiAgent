# HAR-10.5c Pursuit 后台行动核对

## 目标

HAR-10.5a 提供持久行动账本，HAR-10.5b 提供 BackgroundRunner caller idempotency receipt。本切片把
checkpoint、行动账本和后台任务回执合并为类型化恢复判定，使 `action_inflight` 不再依赖中文状态文本或
人工猜测。

本切片只覆盖 Pursuit 的 `background_run`。同步 shell、browser、subagent 和外部 API 仍然 fail closed。

## 权威输入

恢复判定只读取三类结构化事实：

1. `PursuitCheckpoint`：运行、轮次和 `action_inflight` 边界；
2. `PursuitActionRecord`：稳定 action key、dispatch token、状态和 background task ID；
3. `BackgroundTask` 与当前 `BackgroundRunner` 所有权：任务状态、caller key、PID，以及进程和 watcher
   是否仍由当前 Runner 同时持有。

PID 存在不等于任务可安全接管。只有当前 Runner 同时持有活进程和未结束 watcher，任务才属于可等待状态。

## 类型化判定矩阵

| 账本/回执状态 | 判定 | 恢复行为 |
| --- | --- | --- |
| 当前轮次没有行动账本 | `legacy_unknown` | blocked，要求核对外部副作用 |
| `prepared` | `all_accounted` | 记为 `abandoned_before_dispatch`，可继续 |
| action 已是 terminal | `all_accounted` | 不重复派发，可继续 |
| 非 background action 已派发 | `non_background_ambiguous` | blocked |
| 找不到 background receipt | `background_task_missing` | blocked |
| Store/所有权读取失败 | `background_store_error` | blocked，不把异常变成盲重试 |
| task caller key 与 dispatch token 不同 | `background_identity_mismatch` | blocked |
| `preparing` 未超过 30 秒 | `background_active` | waiting，重建等待记录 |
| `preparing` 超过 30 秒 | `stale_preparing` | blocked |
| `running` 且当前 Runner 持有进程和 watcher | `background_active` | waiting，重建等待记录 |
| `running`、PID 存在但 Runner 未持有 | `orphan_running` | blocked |
| `running`、PID 不存在 | `stale_running` | blocked |
| completed/failed/cancelled/timed_out | `all_accounted` | 回写 terminal ledger 后继续 |

同一轮存在多个行动时，任何 ambiguous/blocker 的优先级高于其他 live wait，避免“一个活任务”遮蔽另一个
未知副作用。

## 恢复事务边界

`decide_background_reconcile()` 是无副作用的纯判定函数。Pursuit 在应用判定前重新校验 lease epoch，随后：

- 将确认未派发的 `prepared` action 单调关闭为 failed/`abandoned_before_dispatch`；
- 将后台 terminal receipt 回写为 action terminal 事件；
- 从真实 BackgroundTask 重建 `PursuitBackgroundWait`；
- 写入 reconcile evidence 和更高 sequence 的 checkpoint；
- blocker 清空旧等待视图并保持 checkpoint 在需要核对的边界。

判定不解析模型输出、工具输出或中文提示词，也不会仅凭 PID 存活自动接管进程。

## 验收证据

- prepared 与已终态 action 可安全继续，prepared 被持久化为 abandoned；
- completed background receipt 回写 terminal ledger，并从新 checkpoint 继续评估；
- 当前 Runner 真正托管的真实后台进程恢复为 waiting；
- stale preparing、stale running、orphan running 均进入明确 blocker；
- receipt 缺失、identity 不一致、损坏 Store 和所有权读取异常均 fail closed；
- 非 background ambiguous action 即使与 live background action 并存，也优先阻断；
- legacy checkpoint 没有行动账本时不盲重试；
- 仅运行 Pursuit/Background 相关小模块测试，不运行全量测试。

## 当前不足与后续

- 行动账本 SQLite 与 Background `tasks.json` 仍不是跨 Store 原子事务；行动状态转换也尚未把 lease epoch
  写入同一 SQL 条件，当前是提交前显式校验而非数据库级 fencing；
- 孤儿 PID 不会自动接管或自动终止，需要 ARC-04 daemon authority；
- 同步 shell、browser、subagent、外部 API 尚无各自可核对的幂等 job contract；
- `preparing` 的 30 秒阈值是机械陈旧判定，不替代 worker heartbeat；
- fresh preparing 需要后续唤醒才能再次判定，HAR-10.2 heartbeat/health monitor 负责持续健康推进；
- UI-18.5 仍需把 reason、lease、checkpoint 和可执行恢复建议呈现给用户。

下一最小跨文档切片优先实现 HAR-10.2a typed runtime/worker heartbeat，为 HAR-10 恢复监控、UI-13 Doctor、
UI-18.5 Recovery UX 和 ARC-04.6 Supervisor 提供共同健康事实；不继续线性扩张 reconcile 到全部执行器。
