# HAR-10.1a 持久化 Run Fencing Lease

## 目标

为 Pursuit、长工具、浏览器任务、Agent worker 和 Runtime worker 提供同一套工作区隔离的单主执行原语。
本切片只建立可靠租约与结果 fencing，不启动 heartbeat、queue、checkpoint 或自动恢复循环。

## 为什么不能复用现有 Lease

- retention worker lease 是全库单例，只保护 Session 清理；没有 run ID 和 epoch；
- Workbench lease 是任务市场认领状态，面向人/Agent 分配，不是执行结果提交 fence；
- Evolution worktree lease 保护隔离变异目录，不能授权 Pursuit、浏览器或普通工具结果；
- SubAgent heartbeat 是进程内观测值，进程退出后不是持久化所有权。

HAR-10 必须共享 Harness 的可靠运行控制权威，但不得把这些局部协议悄悄扩展成另一种语义。

## 领域合同

`HarnessRunLease` 的稳定主键是 `(workspace_root, run_kind, run_id)`：

- `run_kind`：`pursuit | tool | browser | agent | runtime`；
- `owner_id`：当前 worker 的稳定身份；
- `epoch`：从 1 开始的单调 fencing token；
- `state`：`active | released`；
- `acquired_at / expires_at / updated_at`：带时区的 UTC ISO 8601 时间。

释放不会删除行。下一 owner（即使 owner_id 与上一轮相同）必须得到更高 epoch，避免旧进程以相同名字复活后
提交陈旧结果。

## 原子状态迁移

### Acquire

- 首次 acquire 创建 epoch 1；
- 未过期的相同 owner 重试保持 epoch 与 acquired_at，只延长、不缩短 expiry；
- 未过期的其他 owner 返回空，不覆盖权威 owner；
- released 或已过期 lease 可接管，epoch 原子加一；
- `now < updated_at` 的倒退时钟请求拒绝写入。

### Renew

只有 `active + owner_id 相同 + epoch 相同 + 未过期 + 时间不倒退` 才能续租。错误 owner、陈旧 epoch、
半开边界 `now == expires_at` 和倒退时间均返回空。

### Release

只有仍存活的精确 owner/epoch 可以把状态改为 released。行与 epoch 保留；重复释放或陈旧释放返回空。

### Fence result

每个结果提交使用稳定 `operation_id` 调用 `record_run_fence_decision()`。Harness 在一个 SQLite 写事务内读取
当前 lease，并持久化 immutable receipt：

- `accepted/current`：owner、epoch、有效期全部匹配；
- `rejected/missing|released|clock_regression|expired|owner_mismatch|epoch_mismatch`：机械拒绝原因；
- 同 operation_id、相同 owner/epoch 的重试返回原 receipt；
- 同 operation_id 被另一 owner/epoch 复用时 fail closed。

上层执行器必须在提交运行状态、证据 cursor 或 destructive action 的安全边界调用 fencing；LLM 文本不能覆盖
该决定。

## 存储与迁移

Harness SQLite schema v11 新增：

- `harness_run_leases`：每个 workspace/kind/run 的最新 owner 与单调 epoch；
- `harness_run_fence_events`：每个 operation 的接受/拒绝审计回执。

迁移保持 v1-v10 表和数据不变。Store 延续 lazy initialization、WAL、`BEGIN IMMEDIATE`、5 秒 busy timeout、
用户状态目录权限收紧和 future schema fail closed。

## 验收证据

- 三个独立 `HarnessStore` 实例并发抢同一 run，恰好一个成功；
- lease 过期后其他 owner 接管，epoch 从 1 增至 2，旧 owner 结果被拒绝并审计；
- release 后同 owner 重抢也获得更高 epoch，旧 epoch 原因是 `epoch_mismatch`；
- 相同 operation retry 幂等，冲突复用 fail closed；
- 错误 kind/ID、布尔 epoch、时钟倒退、过期续租均被拒绝；
- 同一 run ID 在不同 workspace 或 run kind 中互不干扰；
- v1/v2/v3/v4/v8/v10 既有库可增量迁移到 v11，原数据仍可读取。

## 当前不足与后续依赖

HAR-10.1a 只交付 lease authority。Pursuit、browser daemon、background runner 和 Agent cluster 尚未逐一接入；
这些接入应按最小垂直切片完成，并把 fence 检查与各自状态提交放进同一可靠边界。HAR-10.2 heartbeat、
HAR-10.3 durable queue、HAR-10.4 checkpoint、HAR-10.5 reconcile 和 HAR-10.6 human interaction 仍未实现。
