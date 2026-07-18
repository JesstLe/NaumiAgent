# EVO-02.5b Patch Intent Journal 与崩溃恢复 v1

## 目标

关闭单文件 Patch Writer 在锁获取、原子替换、postflight 和回执提交之间的进程崩溃窗口。任何未完成写入
都必须能在下一次 runtime 启动时机械判断并回到 baseline，不能依赖模型猜测、bypass 或人工阅读源码。

本切片仍只处理单文件事务；它先建立未来多文件事务必须复用的持久意图、CAS 状态和恢复证据。

## 持久状态机

`EvolutionPatchJournalStore` 使用现有 runtime SQLite，按 Lease 保持一个当前 journal：

```text
prepared -> replaced -> committed
    |           |
    +-----------+-> rolled_back
    +-----------+-> recovery_failed
```

- `prepared`：baseline backup 已在 SQLite 事务中提交，文件尚未承诺已替换；
- `replaced`：同目录 `os.replace` 与目录同步已完成，但 postflight/Receipt 尚未提交；
- `committed`：postflight 与防篡改 Write Receipt 均已持久化；
- `rolled_back`：文件已是 baseline，backup BLOB 已清除；
- `recovery_failed`：目标、backup 或 binding 无法机械证明，保留 backup 并停止自动动作。

Journal 绑定 Contract、Lease、Source Snapshot、Mutation Plan、Guard Receipt、worktree、target、before/after
摘要、mode、attempt 和 max attempts。`journal_sha256` 覆盖全部公开字段；SQLite row identity/state 与 JSON
双重比对，backup BLOB 另以 SHA-256 校验。

## 写入顺序与崩溃窗口

Writer 在 Lease 排他锁内执行：

1. 处理已提交 journal 的幂等 replay，或拒绝尚未恢复的活动 journal；
2. 重跑 Static Guard 并要求 fresh Receipt 完全相同；
3. 读取 baseline 后持久提交 `prepared + backup`；
4. 原子替换目标并 CAS 到 `replaced`；
5. 执行摘要与 Git scope postflight；
6. 构建 Write Receipt，CAS 到 `committed`，清除 backup；
7. 任一步普通异常均恢复真实字节并 CAS 到 `rolled_back`。

`BaseException`/进程终止可能跳过进程内回滚，但 journal 状态和 before/after 摘要仍足够让启动恢复做决定。
替换已发生但 `mark_replaced` 尚未提交时，journal 仍是 `prepared`；恢复不能仅信状态，而要读取目标摘要。

## 锁与孤儿恢复

锁文件是最多 4 KiB 的严格 JSON，包含随机 owner token、PID、hostname、Lease、worktree 和时间。正常释放
必须匹配 token。`EvolutionPatchRecoveryCoordinator`：

- 活 PID：`deferred/writer_locked`，不删除；
- 异机 owner：`deferred/remote_lock_owner`，不删除；
- 损坏或 binding 不一致：fail-closed，不删除；
- 同机死 PID：先接管为新的 owner token，再执行恢复；
- 在 journal 写入前崩溃形成的孤儿锁：扫描受管 worktree storage，验证文件名与锁内容后清除，记录
  `orphan_lock_removed`。

PID 被系统复用时会保守地延后恢复，不会误杀新进程。后续 heartbeat/daemon owner identity 可以进一步
缩短这种延后，但不能放宽 fail-closed 边界。

## 恢复判定

对 `prepared/replaced` journal：

- target 等于 before：不写文件，标记 `rolled_back/recovered_before_replace`；
- target 等于 after：使用校验后的 backup 原子恢复，或删除未完成 create，再验证 before；
- target 为第三种摘要、symlink、路径越界、backup 损坏：标记/报告失败，不触碰目标；
- committed journal 不进入恢复扫描，Writer replay 会重新验证 postflight 并返回持久回执。

损坏 row 会变成脱敏 `journal_corrupt` 扫描结果，不阻断其他有效 journal 的恢复，也不会把 backup、正文、
数据库路径或内部异常写入 UI。

## Runtime 与 UI

`AgentEngine.start_long_running_services()` 在 Session reconciliation 和 retention worker 之前运行 Patch
Recovery。结果保存在 Engine 状态，并以无源码摘要同步：

- TUI 启动状态：完成/总数、失败、延后；
- 新 terminal UI ready/status payload：rolled back、already baseline、orphan lock、deferred、failed、
  filesystem changed 与去重 failure codes；
- 新 UI 对 payload 做严格计数一致性校验，有恢复事实时关闭空白欢迎页并显示带 info/warning/error 色彩的
  “实验补丁恢复”时间线卡片。

## 验收证据

- 真实 SQLite + Git worktree 覆盖 prepared 与 replaced 两个崩溃窗口，均恢复 baseline 与 clean status；
- journal 已 prepared 但替换前写入失败时，确认 baseline 后立即终结为 rolled_back，无需重启恢复；
- live lock 延后、dead lock 接管、journal 前孤儿锁清理均有真实文件测试；
- backup 篡改只产生 `journal_corrupt`，目标 after 字节保持不变；
- rolled-back journal 接受预算内新 Guard/正文作为下一 attempt，达到 Plan max attempts 后拒绝；
- committed replay 返回同一 Receipt，backup 已清除；
- 旧表缺少 `updated_at` 时增量迁移，不要求删除 runtime DB；
- Engine 启动顺序、TUI 状态、新 UI payload/协议/可视卡片均有聚焦测试。

## 当前不足与后续

- SQLite 与文件系统无法形成单一硬件事务；本实现依靠持久 intent + 摘要状态机消除不确定动作，而不是
  声称跨介质 ACID；
- committed/rolled-back journal 当前保留为当前 Lease 的恢复事实，尚未接入 Lease cleanup/retention；
- 单文件恢复器仍只消费单文件 journal；多文件 write-set 的持久事实、逐文件 phase 与反向 CAS 已由
  EVO-02.5c1 建立，但尚未接入文件 Writer/Recovery；
- Windows 文件占用、断电、磁盘满和杀进程矩阵尚未达到 A4/A5，当前证据是 macOS 真实文件系统；
- 完整 diff/API surface postflight、HAR-08 RED/GREEN 和 EVO-02.7 Mutation Receipt 尚未实现。

EVO-02.4b 已补真实多文件 Proposal/Contract/Plan scope，EVO-02.5c1 已补多文件 write-set journal
contract。下一切片是 EVO-02.5c2 Guard-bound Multi-file Writer 与 Recovery；不得把当前单文件 Writer
循环调用并宣称为多文件原子事务。
