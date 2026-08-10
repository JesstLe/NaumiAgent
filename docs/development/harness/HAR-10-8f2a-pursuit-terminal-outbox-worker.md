# HAR-10.8f2a Pursuit 终态 Outbox 自动恢复 Worker

## 目标

HAR-10.8f1 已保证完整终态 checkpoint 必然和 pending outbox 同事务出现，但仍只能依靠人工
`/pursue reconcile <attempt-id>` 收口。本切片增加默认启用、跨进程 fencing、有界扫描并可安全停止的
自动 worker：

```text
pending outbox
  -> durable dispatch claim(owner digest, epoch, expiry, attempt count)
  -> HAR-10.8e heartbeat + RunLease + higher epoch fence
  -> mechanical PursuitStore reconciliation
  -> attempt + receipt + outbox + dispatch atomically delivered

temporary blocker/error
  -> exact owner/epoch release
  -> durable exponential backoff
  -> later claim with higher dispatch epoch
```

Outbox claim 只控制扫描器调度；真正允许收口旧执行者的权威仍是 HAR-10.8e 的 Harness RunLease 和
accepted fence decision。两套 epoch 不能互相冒充。

## Durable dispatch authority

新增：

- `pursuit_terminal_outbox_dispatch`：每个 outbox 一条最新调度快照；
- `pursuit_terminal_outbox_dispatch_events`：append-only digest chain；
- `PursuitTerminalOutboxDispatch`：frozen schema v1。

状态机：

```text
idle(next_attempt_at)
  -> claimed(owner_sha256, epoch + 1, attempt + 1, expiry)
  -> idle(backoff, last_failure_code)
  -> claimed(higher epoch after backoff or expired takeover)
  -> delivered
```

关键不变量：

- Store 只保存 owner SHA-256，不保存 Runtime owner credential；
- claim 在 `BEGIN IMMEDIATE` 中按 outbox `(created_at, outbox_id)` FIFO 选择；
- live claim 不可被第二 Store 抢占；到期后 takeover 单调增加 epoch 和 attempt count；
- release 必须匹配 exact owner/epoch，且 claim 尚未到期；旧 owner 不能覆盖 takeover；
- retry delay 为 `min(max, base * 2^(attempt-1))`，持久写入 `next_attempt_at`；
- attempt/outbox delivered 时，dispatch 在同一 PursuitStore 事务进入 delivered，不依赖 worker 后续 ACK；
- dispatch 主表、事件链、pending outbox 首事件和 delivered outbox 逐次复验。

## 兼容迁移

10.8f1 数据库首次由新 Store 打开时创建 dispatch 表。缺少 dispatch 的既有 outbox 会在同一初始化事务
中认证并补齐：

- pending outbox -> idle dispatch；
- delivered outbox -> idle 首事件后立即 delivered；
- 最多迁移 10000 条，超过时失败关闭，不进行无界启动扫描；
- 不从 admitted attempt 猜测创建 outbox，只有 10.8f1 已持久的 outbox 能迁移。

## Worker 运行合同

`PursuitTerminalOutboxWorker.run_once()` 每轮最多处理配置的 `scan_limit`（默认 20）：

1. claim 最老 due/expired 记录；
2. 调用同源 `reconcile_pursuit_recovery_attempt()`；
3. 若 outbox 已 delivered，计入成功；
4. 若健康 heartbeat、live lease、宽限期、证据暂缺或其他非终态结果，释放 exact claim 并退避；
5. 调用异常也尝试安全 release；若 claim 已过期，由下一 owner takeover，旧 owner不再写状态。

每轮无记录时指数增加空轮等待，达到配置上限；有记录时恢复基础间隔。周期 loop 支持 wake，stop
不会强制取消进行中的 fencing/reconcile，而是等待当前事务收口后退出，避免 shutdown 制造新的半状态。
周期轮与 Engine 暴露的显式 `run_once()` 共用进程内 single-flight 锁；空轮指数的计算指数也有固定上限，
因此并发触发不会重叠执行，长期空闲不会产生无界大整数运算。

## Engine 生命周期

- `AgentEngine.start_long_running_services()` 在其他持久恢复后执行一次 bounded startup pass，再启动周期 loop；
- `AgentEngine.shutdown()` 等待 worker drain；
- config `harness.pursuit_terminal_outbox.enabled=false` 可关闭 startup/periodic 自动消费；
- 默认启用，默认策略：30 秒周期、20 条/轮、60 秒 dispatch claim、30 秒 admission grace、
  5..300 秒 retry backoff、最多 300 秒空轮 backoff。

配置示例：

```yaml
harness:
  pursuit_terminal_outbox:
    enabled: true
    interval_seconds: 30
    max_empty_backoff_seconds: 300
    claim_lease_seconds: 60
    scan_limit: 20
    reconcile_grace_seconds: 30
    retry_base_seconds: 5
    retry_max_seconds: 300
    max_attempts: 8
    jitter_ratio: 0.1
```

## 验收标准

- 真实 PursuitStore + HarnessStore 的 expired lease 自动推进更高 epoch 并 delivered；
- live Harness lease 不被收口，dispatch 写入 durable backoff；
- 16 路跨 Store 并发 claim 只有一个 owner 成功；
- claim 到期 takeover 后旧 owner release 被拒绝；
- backoff 到期前不可重领，到期后 epoch/attempt 单调增加；
- 10.8f1 pending/delivered outbox 关闭重开后自动补齐正确 dispatch；
- dispatch 快照或事件摘要篡改失败关闭；
- shutdown 等待正在进行的 reconcile，不取消到一半；
- 周期轮与显式轮串行执行，超大空轮计数仍保持 capped backoff；
- Engine startup pass 早于周期 worker 启动，配置关闭时不启动；
- 只运行 config、Pursuit recovery、Engine lifecycle 小模块，不运行全量测试。

## 自我审视与未完成项

- 本切片交付时没有 dead-letter；后续 HAR-10.8f2d 已增加独立失败预算、追加式死信权威与领取排除，
  但人工处置和 retention 仍需要独立权限/回执设计。
- HAR-10.8f2b 已把 bounded backlog 和 worker snapshot 投影到 Bridge/Goal/New UI/TUI，前端不扫描
  SQLite；控制动作和 push stream 仍未实现。
- HarnessStore fencing 与 PursuitStore dispatch 仍非跨库事务，因此语义是 at-least-once 收敛；
  reconciliation 与 outbox delivered 在 PursuitStore 内是幂等原子事实。
- 仍未完成 kill-at-every-write-point 全矩阵、多主机时钟漂移、Linux/Windows 进程杀死和 24 小时 soak。
