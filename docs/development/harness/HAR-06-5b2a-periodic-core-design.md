# HAR-06.5b2a Retention 周期服务核心

## 交付边界

本切片把 HAR-06.5b1 的单轮执行器包装为可启动、停止、唤醒和观测的周期服务，但不在进程启动
时自动运行。这样可以先验证跨实例权威和关闭语义，再由 6.5b2b 统一接入 CLI/New UI/TUI/API
启动生命周期。

已交付入口：

- Engine：`start_session_retention_worker()`、`stop_session_retention_worker()`、
  `wake_session_retention_worker()`、`session_retention_worker_snapshot()`；
- CLI/New UI/TUI：`/history retention-worker [status|start|stop|wake]`；
- Agent：只读状态通过 `session_history(action="retention_worker_status")`，生命周期控制通过破坏性
  `session_retention_worker(action="start|stop|wake")`。

## 默认关闭与配置

```yaml
memory:
  session_retention:
    periodic_enabled: false
    interval_seconds: 300
    max_empty_backoff_seconds: 1800
    worker_lease_seconds: 60
    standby_retry_seconds: 15
    jitter_ratio: 0.1
```

`periodic_enabled=false` 时 Engine 拒绝启动 worker；显式修改配置后控制命令才能启动。租约时间必须
大于单轮 `max_runtime_seconds`，空轮最大退避不得小于基础周期，抖动限制为 0–50%。

## 跨实例租约

Harness DB v7 增加单行 `harness_retention_worker_leases`：

- `lease_name` 固定为 `session_retention`；
- acquire 使用单条 SQLite upsert，只有同 owner 或已过期租约可以更新；
- renew 只允许未过期且 owner 匹配的持有者；
- release 带 owner 条件，旧 owner 不能释放接管后的新租约；
- 等待期间每 `lease_seconds / 3` 心跳续租，续租失败立即转为 standby，不再启动下一轮。

租约约束调度权威，不被当成跨数据库事务锁。如果进程发生长时间 stop-the-world 并超过租约，新的
owner 可以接管；旧进程恢复后的重复操作仍由协调 request id、retention 终态和 Session Store
`DELETE ... WHERE status='archived'` 保证幂等与不误删 active Session。

## 调度与关闭

- 启动后立即竞争租约；失败进入 standby，按独立间隔重试；
- 有候选的轮次恢复基础周期；连续空轮按 `2^n` 退避到上限，并应用有界随机抖动；
- wake 会打断等待，立即开始下一次租约检查/执行；
- stop 设置与单轮执行器共用的 cancel event，等待当前协调持久化安全状态，然后释放租约；
- start/stop 幂等，同一 Engine 不会创建两个本地 loop；
- 原始异常不进入状态，只有封闭错误码 `lease_acquire_failed|lease_renew_failed|`
  `lease_release_failed|pass_failed`。

## 指标快照

快照包含 worker state、租约、轮数、完整删除、安全重试、worker 失败、连续空轮、下次等待、最近
轮状态、错误码和时间。CLI/New UI/TUI 共用同一中文 renderer，不把 retry 或 standby 显示成成功。

## 验收证据

- 两个真实 HarnessStore 并发 acquire 只有一个成功；租约到期后 loser 可接管；
- 两个真实 PeriodicService 并发 run cycle 只有持租约实例执行；
- wake 打断长等待，租约丢失转 standby，空轮退避和非空轮复位确定；
- stop drain 当前 pass 并且仅释放自身租约；
- Engine 默认关闭、配置门、控制委派和 shutdown stop 均有聚焦测试；
- v1-v6 Harness 数据库升级至 v7 后原数据不丢失。

## HAR-06.5b2b 已完成

长期运行入口现已在完成启动恢复后启动明确启用的 worker，退出时统一 drain；一次性
`naumi run <task>` 不启动周期 worker。New UI 心跳 `runtime/status` 与 API health 暴露同一类型化
快照，详见 `HAR-06-5b2b-long-running-lifecycle-design.md`。
