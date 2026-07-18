# EVO-02.5c1 多文件 Write-set Journal Contract v1

## 目标与边界

本切片为真正的多文件补丁建立一个持久、可恢复、可证明顺序的 write-set 事务契约。它解决“未来的
Writer 在写第几个文件时崩溃，恢复器应信什么”的问题，但**不执行任何文件写入**，也不把
`execution_ready` 或 `write_authorized` 提升为 true。

禁止用循环调用单文件 `EvolutionPatchWriter` 冒充多文件事务。多文件 Writer 与 Recovery 必须消费本契约，
共同使用同一 Lease 排他锁，并在写第一个文件之前持久化整个 write-set。

## 权威来源与确定性顺序

`EvolutionPatchSetStore.prepare()` 同时校验真实 Contract、active Lease、Source Snapshot、Mutation Plan 和
通过的 Static Guard Receipt。文件集合必须：

- 包含 2..16 个文件；
- 与 Mutation Plan 的完整授权集合相等，不允许漏项或扩项；
- 使用 Static Guard Receipt 的稳定路径排序作为唯一 apply 顺序；
- 为每个 modify 保存与 `before_sha256` 一致的真实 baseline bytes；
- 为 create 明确保存“无 baseline”，不得伪造 backup；
- 保存每个文件的 operation、before/after digest、mode、phase 和独立 `fact_sha256`。

transaction ID 由 Contract、Lease、Snapshot、Plan、worktree、路径、operation 和 before authority 生成。
同一 Plan 的失败重试可以绑定新的 Guard/after digest，但不能改变文件集合、顺序、操作类型或 baseline。

## SQLite 持久结构

现有 runtime SQLite 增量创建两张独立表：

- `evolution_patch_sets`：每个 Lease 唯一 transaction，保存带摘要的完整 JSON、state、更新时间和终态回执；
- `evolution_patch_set_backups`：按 `(transaction_id, file_index)` 保存 baseline BLOB，外键级联清理。

`prepare()` 在一个 `BEGIN IMMEDIATE` 事务中写入 transaction 与全部 backups。读取时同时验证：

1. JSON 内 `transaction_sha256`；
2. 每个 file fact 的 `fact_sha256`；
3. SQLite row 与 JSON 的 transaction/Lease/state/time binding；
4. backup 行集合与 `backup_retained` 声明完全相等；
5. 每个 backup BLOB 的 SHA-256。

错误只返回脱敏 failure，不包含源码或 backup 内容。
`scan_recoverable()` 按更新时间枚举所有活动 transaction；单条 JSON/BLOB 损坏会转成
`patch_set_corrupt`，不会阻断其他可恢复事务，也不会把正文写入日志或 UI。

## 状态机与 CAS

```text
prepared -> applying -> applied -> committed
    |           |          |
    +-----------+----------+-> rolling_back -> rolled_back
                                      |
                                      +-> recovery_failed
```

- `applied_count` 是前向游标。`mark_file_replaced(index)` 只接受 `index == applied_count`；最后一项完成后进入
  `applied`。
- `rollback_cursor` 是反向游标。`begin_rollback()` 从最后一个 index 开始；
  `mark_file_rolled_back(index)` 只接受当前 cursor，然后递减。
- 即使某个尾部文件尚未写入，Recovery 仍需先机械确认它保持 baseline，再按逆序标记。这样不能跳过任何
  write-set 成员。
- 只有 cursor 到 `-1` 且全部 file phase 为 `rolled_back`，才能进入终态并清除 backups。
- 只有全部文件为 `replaced` 的 `applied` 状态才能 commit；commit 持久化最大 256 KiB 的摘要回执并清除
  backups。
- recovery 无法证明目标时进入 `recovery_failed` 并保留 backups，禁止继续自动写入。

SQLite 更新以旧的完整 `transaction_json` 作为 compare-and-swap token；同状态的多步 apply 也不能被陈旧
调用覆盖。并发 `prepare` 由 Lease UNIQUE + `BEGIN IMMEDIATE` 收敛为同一 transaction。

## 重试预算

完整逆序恢复后的 `rolled_back` transaction 可以在 `Mutation Plan.max_attempts` 内重新 prepare：

- transaction ID、created_at 和 baseline authority 保持不变；
- attempt 递增；
- 新 Guard ID、Receipt digest 和 after digests 被重新签名；
- backups 在同一 SQLite 事务中重新持久化；
- 达到预算后返回原 `rolled_back` 事实，不创建新的 attempt 或写入权限。

## 验收证据

- 真实 Feedback → Proposal → Contract → Lease → Snapshot → 双文件 Plan → Guard 链路生成 write-set，无伪造
  Plan/Receipt；
- prepare 后主工作树与隔离 worktree 字节、Git status 均未变化；
- 两个 baseline backups 与 mode/digest/phase 完整持久化；
- 乱序 apply 被拒绝，`0 -> 1` 才能进入 applied；未完成时不得 commit；
- rollback 必须 `1 -> 0`，未完成时不得进入 rolled_back；
- transaction JSON 与 backup BLOB 篡改均 fail-closed，异常不泄漏正文；
- committed 回执可完整复读且 backups 已清除；
- 预算内 revised Guard 可重试，预算耗尽后保持 rolled_back；
- 同 Lease 两个线程并发 prepare 只产生一条 transaction 与两条 backup。

## 当前不足与下一切片

- 本切片没有任何多文件落盘 API，`write_authorized=false`、`execution_ready=false` 是有意安全边界；
- single-file journal 与 write-set 目前尚未做跨表互斥，必须由 2.5c2 在同一 Lease 锁内双向检查；
- 进程崩溃、文件摘要判定、严格逆序恢复和 UI 状态尚未连接到 write-set；
- SQLite 与文件系统仍依赖 intent + digest 恢复语义，不宣称跨介质 ACID；
- Windows 文件占用/断电/磁盘满矩阵仍待平台验证。

EVO-02.5c2a 已实现 Guard-bound Multi-file Writer、普通异常逆序回滚和单/多事务互斥。下一切片
`EVO-02.5c2b` 实现启动 Recovery Coordinator 与 UI 状态；它必须复用本 write-set contract，不能另造状态
机。完成 2.5c2b 后再进入 EVO-02.6b 完整 diff/API surface postflight。
