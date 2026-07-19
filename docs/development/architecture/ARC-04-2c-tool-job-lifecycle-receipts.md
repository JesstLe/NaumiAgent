# ARC-04.2c ToolJob Lifecycle Receipts

## 1. 交付目标

在真实 Shell worker 出现之前，先冻结“任务何时可能产生副作用、何时允许重试、何时必须人工/监督器
reconcile”的持久事实。Runtime 与 Worker 只能沿同一条防篡改生命周期推进：

`admitted -> dispatched -> running -> succeeded|failed|cancelled|unknown`

`admitted -> cancelled` 是唯一可证明 `side_effect=none` 的终态。`dispatch` 一旦持久化，后续状态不得再声明
无副作用；Worker 丢失、transport 断连或进程结果无法证明时必须进入 `unknown + possible`，禁止自动重放。

## 2. 持久化合同

`ToolJobStore` 升级到 SQLite schema v2，并在同一数据库维护：

- `tool_jobs`：不可变 Job contract、当前单调状态、latest sequence 与 latest receipt；
- `tool_job_lifecycle_events`：按 `job_id + sequence` 追加的完整 receipt 链；
- 每条 receipt 绑定 Worker id/instance/epoch、dispatch id、前序状态/摘要、结果码、exit code、输出与
  artifact manifest 摘要、side-effect 分类和完整 SHA-256；
- 原始 command、env、stdout/stderr、artifact 内容和 secret 不进入 Store。

v1 数据库只允许包含已验证的 admitted contract。迁移会为每个旧 Job 生成确定性 genesis receipt，再原子切换
到 v2；未知表、索引/contract/event 篡改、未来 schema 均 fail closed。

## 3. Authority 与顺序保证

- `ToolJobAuthority.dispatch()` 在返回给 transport producer 前重新执行 ARC-04.2b authority validation，随后先
  持久化 `dispatched`；只有首次 transition 返回 `should_send_payload=true`，未落盘或幂等重试不得发送命令；
- `ToolJobLifecycleAuthority` 只接受合同绑定且仍为 Worker Registry active generation 的 Worker incarnation，
  并核对 durable dispatch id；takeover 后旧 Worker 不能提交迟到终态；
- 状态只允许前进；终态不可改写；相同 transition retry 返回首次 receipt，不同终态或结果冲突拒绝；
- lifecycle 时间不得倒退，事件数量、索引列、前序状态和摘要链每次读取都重新验证；
- `list_recovery_required()` 只列出进程崩溃后仍为 `dispatched|running` 的任务。恢复方必须先查证 Worker/transport
  事实，再以 latest receipt digest 作为 optimistic fence 写 `unknown`，不能重新 dispatch；Worker takeover 后
  旧 Worker 不能提交终态，但 Runtime 仍可用该 recovery authority 安全关闭歧义任务。

## 4. 验收证据

- 真实 admission authority 完成 admit -> dispatch -> running -> succeeded，关闭 Store 后完整恢复四段 receipt；
- 8 个独立 Store 并发提交同一终态，只追加一条终态事件；不同终态内容、状态倒退、错误/已被 takeover 的
  Worker incarnation 和 dispatch id 均 fail closed；
- dispatch 后崩溃可从 recovery query 找回，写入 `unknown + possible` 后不再进入自动恢复队列；
- dispatch 前取消保持 `side_effect=none` 且幂等；
- v1 -> v2 真实 SQLite 迁移保留 Job identity 并补齐 genesis receipt；中间事件篡改可被完整链验证发现；
- Runtime Composition 为 Engine 注入同一个 Store 上的 admission/lifecycle authority。

## 5. 诚实边界与下一步

- 本切片没有 daemon transport、原始 payload delivery、OS process、artifact writer 或 sandbox；receipt 证明状态
  机没有被 Runtime 悄悄改写，不证明命令真的执行；
- `unknown` 当前是阻断自动重试的终态，尚无 Supervisor 证据驱动的二阶段 reconcile/override；
- 没有跨进程身份认证或 receipt 签名，SHA-256 只提供内容完整性；
- ARC-04.3a/3b 已消费本合同，完成 authenticated local non-PTY transport、最小 OS 隔离与一次性 admission；
  HAR-08.4a/4b 已通过相同 ToolJob/lifecycle authority 跑通生产 Profile check。Suite/Batch 或自进化 cohort
  后续必须复用本身份链，不得另造任务或 subprocess 权威。
