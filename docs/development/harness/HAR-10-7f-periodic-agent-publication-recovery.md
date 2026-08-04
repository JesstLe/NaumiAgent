# HAR-10.7f 周期 Agent Publication 恢复 Worker

## 1. 目标

ARC-04.5d2b 已把 Agent 终态结果提交到加密 publication outbox，并在 Engine 启动时执行一次有界恢复。
但运行时若在 terminal commit 之后、durable result inbox 投递之前发生短暂 Store/进程错误，只能等待
下一次启动。HAR-10.7f 增加 Engine 所有的常驻周期 worker，使同一进程可以在不中断会话的情况下恢复
这类 post-commit gap。

该 worker 不执行模型、不重放 Agent 工具，也不修改 Job 的成功/失败裁决。它只消费已经存在且经过认证的
terminal publication，并继续走既有 `claim -> recover content -> inbox delivery` 权威链。

## 2. 已实现范围

- `AgentPublicationRecoveryWorker`：
  - 单进程 `asyncio.Lock` 串行每次 pass；
  - 每轮最多扫描配置的 `scan_limit`；
  - 通过 `SubAgentManager.recover_pending_publications()` 使用 SQLite claim lease/epoch fencing；
  - 空轮指数退避、失败指数退避和有界 jitter；
  - `start/stop/wake/run_once/snapshot` 生命周期端口；
  - shutdown 设置 stop/wake 并等待 loop 退出，不遗留后台 task。
- Engine 生命周期：
  - 默认启用；
  - 启动后台服务前先完成一次同步 publication pass；只有该 pass 返回后才启动周期 loop；
  - 配置关闭周期 worker 时仍保留原有一次性启动恢复，避免降低既有 crash recovery；
  - live publication delivery 失败后由 Manager 调用窄 `wake()` 端口，缩短恢复等待；
  - Engine shutdown 在销毁 SubAgentManager 前 drain worker。
- 有界、低敏 snapshot 只包含状态、累计计数、连续空轮/失败轮数、下一延迟、稳定 failure code 与时间；
  不包含 task/context/response、owner ID、凭据或原始异常。

## 3. 权威与执行链

```text
Agent terminal commit
  -> encrypted publication PENDING
  -> live inbox delivery
       success -> durable delivery + PUBLISHED
       failure -> release claim + wake periodic worker

startup pass / periodic wake / interval
  -> AgentPublicationRecoveryWorker.run_once()
  -> SubAgentManager.recover_pending_publications(limit)
  -> AgentJobStore.claim_next_publication()
  -> authenticated terminal content recovery
  -> atomic durable result inbox delivery
  -> best-effort in-process notification
```

durable inbox 写入是完成边界；进程内 message bus notification 只是唤醒提示。notification 失败不会把已完成的
durable delivery 回滚成 pending，也不会再次写 inbox。

## 4. 配置合同

```yaml
harness:
  agent_publication_recovery:
    enabled: true
    interval_seconds: 30
    max_empty_backoff_seconds: 300
    max_failure_backoff_seconds: 300
    scan_limit: 100
    jitter_ratio: 0.1
```

- interval 最小 100ms、最大 24h；
- empty/failure 最大退避不得小于 interval，且最多 7 天；
- scan limit 为 1..1000；
- jitter 为 0..0.5；测试可设为 0 获得确定性。

配置由 Pydantic 在启动前失败关闭。运行时不能把无效配置静默裁剪为另一套策略。

## 5. 并发、失败与幂等

- 同一 worker 的并发 `run_once()` 被锁串行，避免同 owner 并行扫描造成不必要的 claim 竞争；
- 多 Runtime 仍由 Store 的 publication claim owner、epoch 和 expiry 决定唯一 live consumer；
- inbox delivery 已存在时返回同一 delivery，不能重复插入；
- Manager 已将 claim/delivery/release 异常收敛为稳定 code，worker 对逃逸异常只记录
  `agent_publication_worker_pass_failed`；
- 连续失败采用 `interval * 2^(n-1)`，连续空轮采用 `interval * 2^n`，两者均受配置上限约束；
- 有真实工作且无失败时恢复到基础 interval；显式 wake 会中断当前等待，但不能绕过 Store fence。

## 6. 用户可见闭环

本切片不新增一套前端状态源。现有 Agent Control `recovery_catalog` 会继续显示 pending/expired publication；
worker 投递成功后，该条目从恢复目录消失，并通过同一 result inbox 出现在“结果”页。Engine 同时提供严格
低敏的 worker snapshot 端口，后续若增加周期 worker 健康投影，New UI/TUI 必须消费该端口，不能自行统计。

## 7. 聚焦验收

- Policy 拒绝无界 scan、非法 jitter 和小于 interval 的退避上限；
- 两个并发 pass 的 Manager 调用峰值严格为 1；
- start 后不重复立即执行 startup pass，wake 可立即触发，stop 等待并进入 stopped；
- 未捕获异常不会泄露原始错误文本，连续失败按上限退避；
- 使用真实 AgentJob SQLite 制造 terminal commit 后的 delivery gap，新 worker pass 将 pending publication
  投递为 PUBLISHED，且 result inbox 只有一条 delivery；
- Engine 的 startup pass 发生在周期 start 之前，配置 gate、显式端口和 shutdown drain 均有局部测试；
- 只运行 worker、Agent publication、Engine lifecycle 与配置小模块测试，不运行全量测试。

## 8. 自我审视与未完成边界

本切片解决“同一运行进程内周期恢复 post-commit publication gap”，没有声称完成 publication 治理：

- HAR-10.7g 已补齐 per-record durable retry budget 与 quarantine/dead-letter，poison publication 不再永久
  阻塞后续 FIFO；exact requeue、放弃和 prune 仍未完成；
- 尚无 retention/ack/prune 操作和历史 worker 健康页；
- notification 是 best-effort，不是持久订阅队列；
- 尚未实现独立 Agent Worker、跨 workspace 公平、Supervisor owner lease 或跨主机 topology；
- A5 kill-at-every-write-point 与长时间 soak 仍需独立验收。

HAR-10.7g 的依赖比较选择先完成 publication quarantine/dead-letter，并坚持不能让周期 worker 在没有 Store
fence 的情况下自行跳过或删除失败记录。下一步重新比较 exact requeue 与独立 Agent Worker owner lease。
