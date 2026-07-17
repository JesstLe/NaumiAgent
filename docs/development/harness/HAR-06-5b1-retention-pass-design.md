# HAR-06.5b1 显式有界 Retention Pass

## 目标与边界

在周期 worker 之前，先交付一轮可由用户明确触发、可取消、有时间预算且可审计的归档清理。入口为：

- CLI/New UI/TUI：`/history retention-run`；
- Agent Tool：`session_retention_run`，标记为 destructive 且需要权限系统授权；`bypass` 模式按
  全权限语义直接执行。

执行器不接受任意 Session ID，也不直接调用 Store delete。它只消费同一时刻生成的 6.5a 计划，
并逐项使用 `LifecycleActor.RETENTION_WORKER` 调用现有
`SessionReconciliationCoordinator`。本切片不启动定时器、不创建常驻线程，也不在启动时自动删除。

## 安全状态机

每项删除有两次策略检查：

1. 规划阶段只选取 `archived` 且非当前会话的候选；
2. 协调器在真正删除前重新读取 Session，retention actor 仍只允许 `archive -> delete`；
3. Session Store 最终使用单条 `DELETE ... WHERE status = 'archived'` 原子提交，消除复核与删除之间
   的 TOCTOU。自定义 Session Port 若没有该能力会失败关闭。

第二次检查也适用于 tombstone 恢复。若请求 prepared 后进程中断，而用户随后恢复 Session：

- Session 不删除；
- `harness_session_reconciliation_terminals` 写入
  `retention_policy_blocked`；
- Artifact GC 标记为无需执行的 completed；
- 已租赁 tombstone 解决，后续恢复扫描排除该终态；
- 回执显示“策略阻止”，不冒充完整删除。

Retention request ID 除 workspace、Session ID、创建时间外，还包含 `archived_at`。同一 Session
以后重新归档会形成新的 archive epoch 和新的协调请求，不会复用已被策略终止的旧请求。

## 有界执行规则

- 顺序严格沿用 6.5a 最久未访问优先计划；
- 会话数与载荷字节预算在规划阶段已经是硬上限；
- `max_runtime_seconds` 默认 10 秒、最大 300 秒；到期会取消当前协调，协调器先持久化安全 tombstone，
  剩余候选不启动；
- 显式 `cancel_event` 与调用方 task cancellation 均传播到当前协调，Session/Harness 两阶段恢复语义
  不被绕过；
- 未预期异常失败关闭并停止本轮，回执不包含原始异常文本；
- `completed`、`retry_scheduled`、`retry_exhausted`、`policy_blocked`、`not_found` 分开计数，只有
  `completed` 可以称为完整删除。

## 配置

```yaml
memory:
  session_retention:
    delete_archived_after_days: 30
    max_archived_session_bytes: 0
    max_sessions_per_pass: 20
    max_bytes_per_pass: 268435456
    scan_limit: 10000
    max_runtime_seconds: 10
```

## 验收标准

- 多候选按计划顺序执行，所有协调结果准确分类；
- 时间预算、显式取消、调用方取消和未预期异常均停止后续候选；
- 取消发生在 prepared 后会形成可恢复 tombstone；恢复前 Session 若已 active，则终态阻止删除；
- v1-v5 Harness 数据库升级到 v6 后原记录、tombstone 和 Artifact GC 数据不丢失；
- 真实 Session Store + Harness Store 完成一轮删除，其他 Session 与跨 workspace 数据不变；
- CLI/New UI/TUI 与 Agent Tool 共用同一中文回执。

## 后续 HAR-06.5b2

周期调度只能调用本单轮接口，并增加单实例租约、抖动、空轮退避、进程关闭 drain 和吞吐指标。
后台 worker 默认关闭，启用配置与启动生命周期必须在 6.5b2 一起交付，不能提前加入无效开关。
