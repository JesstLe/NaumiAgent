# ARC-06.1a Worker Capacity 原子 Reservation

## 交付目标

现有 `WorkerHealthReport.active_jobs` 只能描述某个观测时刻，两个调度者可能同时看到最后一个空位并双双
放行。本切片在 Runtime-owned Worker Registry 内建立持久 reservation authority，使容量判断与占位在同一
SQLite `BEGIN IMMEDIATE` 事务内完成。它是 ARC-06 admission control 与 HAR-10.7 cluster scheduling 的
共同前置，不冒充完整 scheduler。

## 数据与不变量

Worker Registry schema 从 v1 迁移到 v2，新增 `worker_capacity_reservations`：

- reservation 绑定 `worker_id + instance_id + epoch + job_id`，不能跨 incarnation 重放；
- 同 worker epoch 的 job identity 唯一，reservation identity 被不同事实复用时拒绝；
- active 数量严格小于合同的 `max_concurrent_jobs` 才能提交新 reservation；
- TTL 为 1–604,800 秒，且不得超过该 worker 的 `max_wall_seconds`；
- 状态只允许 `active/released/expired/fenced`，终态必须同时记录时间和原因；
- release 精确校验 owner，且仅在相同 reason 下幂等；终结 reservation 不可复活；
- TTL 以解析后的 aware datetime 比较，不使用可能受时区偏移影响的字符串排序；
- higher epoch takeover 或 revoke 在同一事务中 fencing 旧 incarnation 的全部 active reservation；
- `capacity_snapshot()` 先原子收割到期项，再返回 maximum/reserved/available 的同一时刻快照。

`bypass` 不参与这层判断：它可以放行工具权限，但不能扩大 worker 合同容量、复活旧 epoch 或绕过 TTL。

## 迁移与失败语义

- 新数据库一次建立 v1 registration 与 v2 capacity 表，最终 `user_version=2`；
- 已知 v1 原位增加 v2 表并保留 registration；未知未版本化数据库和未来版本继续 fail closed；
- SQLite/文件/反序列化问题统一转为中文 `WorkerRegistryStoreError`；
- capacity exhaustion 使用独立 `WorkerCapacityExhaustedError`，调度器后续可据此排队，而不是误判 worker 故障。

## 聚焦验证

- 三个独立 Store 并发争抢 capacity=2：恰好两个提交，一个 `capacity exhausted`；
- release 后立即可重新占位，同 reason 重放幂等，不同 owner/reason 拒绝；
- TTL 到期释放容量，迟到 release 拒绝；TTL 超过 resource envelope 拒绝；
- epoch takeover fencing 旧 reservation，旧 owner 不能提交终态；
- active reservation 索引列被篡改时 snapshot/reserve fail closed，不把损坏行误算为空位；
- v1→v2 原位迁移后 registration 不丢失，并可立即 reserve；
- 当前真实 darwin/arm64 宿主机合同完成注册、占位和 snapshot：`reserved=1, available=1`。

验证命令只覆盖相关模块：

```bash
python3 -m ruff check \
  src/naumi_agent/daemons/worker_registry.py \
  tests/unit/test_worker_registry.py tests/unit/test_store_catalog.py
python3 -m pytest -q tests/unit/test_worker_registry.py tests/unit/test_store_catalog.py
```

## 明确未完成

- 还没有 priority、deadline、公平队列或 starvation aging；
- 还没有 workspace exclusive/shared reservation、provider/tool 多级 bulkhead 或预算预留；
- 当前一次性 Shell worker 已由 Tool run lease 串行且每任务创建新 incarnation，本切片不重复接入；
- 下一步应优先让持久 Agent/Browser worker 在 dispatch 前 reserve、终态/取消时 release，再建立有界等待队列；
- 24h soak、跨进程 crash recovery 指标和 UI 调度可观测性仍未交付。
