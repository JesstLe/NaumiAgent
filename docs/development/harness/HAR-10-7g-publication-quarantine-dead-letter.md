# HAR-10.7g Agent Publication 重试预算与隔离权威

## 1. 目标与依赖裁决

HAR-10.7f 已让 terminal publication 在同一进程内周期恢复，但 FIFO 首条如果持续无法认证或写入
result inbox，worker 每轮都会在该条失败后停止，后续健康结果永远无法投递。直接在内存中跳过失败项会
丢失失败身份、重试次数与裁决依据，也无法跨进程证明该项为何不再进入 claim。

本切片先完成 durable quarantine/dead-letter，而不提前实现独立 Agent Worker。独立 Worker 仍依赖
注册、认证 transport、owner lease、Supervisor、物理 slot reservation 与 drain；隔离权威只依赖当前已完成的
publication outbox、claim epoch、周期 worker 和 Agent Control，因此是解除真实 FIFO 阻塞的最小前置。

## 2. AgentJob Store schema v6

schema v6 新增 `agent_job_publication_quarantines`。每个 publication 最多一条隔离事实，字段包括：

- publication/job/request/result identity；
- 失败时的 publisher owner、claim epoch 与实际 attempt count；
- 配置的 `max_attempts`；
- 稳定低敏 failure code 与 quarantine time；
- 被隔离后 publication latest receipt digest；
- quarantine receipt digest、序列化 receipt 与 HMAC authentication。

表对 publication/job 使用 `ON DELETE RESTRICT` 外键；publication、job、result、publication receipt 与
quarantine receipt 均有唯一约束。Store 每次初始化会复核精确列、目录索引、唯一约束和外键。v5→v6
只增加隔离表，不改写既有 Job、publication、delivery 或 result inbox。

## 3. 原子隔离状态机

```text
claimed(owner, epoch, attempt)
  -> delivery failure
  -> attempt < max_attempts: release to pending and stop this pass
  -> attempt >= max_attempts:
       append publication receipt: pending(agent_publication_quarantined)
       insert authenticated quarantine receipt
       commit both facts atomically
       continue to the next FIFO publication
```

`quarantine_publication()` 必须同时复验 live owner、exact claim epoch、未过期 lease、实际 attempt count 和
retry budget。相同 owner/epoch/budget/failure code 重放返回同一隔离回执；任一事实漂移均失败关闭。
隔离不会删除 terminal payload、publication event chain 或 result authority，也不会把失败结果冒充 published。

## 4. 防伪造阻塞与有界扫描

`claim_next_publication()`、恢复目录与恢复目录 catalog 在使用 `NOT EXISTS quarantine` 跳过记录之前，先
认证有界 quarantine catalog：

- 最多 10000 条，超过安全上限明确失败；
- 每条重验 quarantine HMAC、表投影、publication latest receipt、job/request/result、epoch 与 attempts；
- 伪造一行 quarantine 不能静默阻塞合法 publication；
- 认证通过的隔离记录不再参与自动 claim，但继续出现在操作目录。

该选择用额外只读验证成本换取 fail-closed。未来若隔离规模增长，应增加独立、带认证 root 的分页索引，
不能通过取消认证来优化性能。

## 5. Worker 与配置

```yaml
harness:
  agent_publication_recovery:
    max_attempts: 5
```

`max_attempts` 为 1..1000，默认 5。attempt 是 Store 在每次新 claim/takeover 时单调增加的 durable 事实，
不是 worker 内存计数。delivery failure 达到预算后，Manager 调用 Store 隔离并继续本轮；隔离失败则 best-effort
release、记录 `agent_publication_recovery_quarantine_failed` 并停止，禁止无证据跳过。

worker snapshot 与恢复 summary 增加累计 `quarantined`，原始异常文本不会进入 snapshot、Store 或 UI。

## 6. New UI 与 TUI 用户闭环

Agent Control 升级为 schema v5：

- summary 增加 `durable_publications_quarantined`；
- recovery state 增加 `publication_quarantined`；
- recovery item 使用 quarantine time、quarantine receipt digest 和稳定 failure code；
- 隔离项优先于 unknown、expired claim 和普通 pending publication 展示；
- New UI 与 Textual TUI 使用红色“发布失败已隔离”，显示尝试次数和原因码；
- New UI 将 durable/异常摘要拆成独立一行，120 列终端不会因普通统计字段挤掉红色隔离状态。

前端只消费 Python authority，不读取 SQLite、HMAC key、owner ID、task/context/response 或原始异常。
本切片不提供 requeue/delete 按钮；没有新的权限回执和 exact requeue fence 前，界面不得通过本地动作解除隔离。

## 7. 聚焦验收

- schema v5→v6 保留既有 publication 并创建精确约束的隔离表；当前 v6 缺表或弱化唯一约束时失败关闭；
- attempt 未耗尽不能隔离，旧 owner/epoch 不能隔离，相同事实重放幂等，漂移重放拒绝；
- 隔离与 publication quarantine receipt 同事务，close/reopen 后读取同一认证事实；
- 篡改 failure code 并重算公开 digest 仍因 HMAC authentication 失败；
- 两条真实 publication 中第一条持续 delivery 失败、第二条健康时，第一条被隔离且同一 pass 继续投递第二条；
- backlog、recovery catalog、Agent Control schema v5、New UI 与 TUI 显示同一隔离计数/状态；
- 常见 120 列 New UI 可见红色隔离摘要；
- 只运行 AgentJob quarantine/migration、publication worker、Agent Control/TUI formatter 和 Node
  protocol/render 小模块，不运行全量测试。

## 8. 自我审视与未完成边界

本切片真实解除 poison publication 对 FIFO 的永久阻塞，但没有完成全部 dead-letter 运维：

- 隔离记录暂时是不可变终态，尚无 exact requeue、人工放弃或 delete/prune receipt；
- 尚无隔离 retention、历史分页、导出和告警 sink；
- `max_attempts` 是全局 worker policy，尚无 provider/sink/failure class 级预算；
- quarantine catalog 当前有 10000 条硬上限，未完成大规模分片/认证 root；
- Agent 仍是 embedded 执行，独立 Worker、Supervisor、跨 workspace 公平与跨主机 topology 未完成；
- A5 kill-at-every-write-point、磁盘满、锁竞争和长时间 soak 仍需独立验收。

下一步应比较 `HAR-10.7h` exact quarantine requeue 与独立 Agent Worker owner lease。若没有用户可恢复需求，
优先推进独立 Worker 的最小注册/认证 transport；不得直接扩张完整 scheduler。
