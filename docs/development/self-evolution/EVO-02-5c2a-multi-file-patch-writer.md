# EVO-02.5c2a Guard-bound Multi-file Patch Writer v1

## 目标与边界

本切片让已通过 Static Guard 的 2..16 文件 write-set 能在专用 Experiment worktree 中按确定性顺序落盘，
并在普通异常时严格逆序恢复全部 baseline。它消费 EVO-02.5c1 的唯一持久状态机，不循环调用单文件
Writer，不写主工作树，不提升 `execution_ready`。

进程终止、断电或 `BaseException` 形成的遗留事务已由 EVO-02.5c2b 启动恢复；本切片负责确保崩溃前已经
留下足够、准确、可扫描的 intent。

## 执行协议

`EvolutionPatchSetWriter.apply()` 在与单文件 Writer 相同的 Lease 锁内执行：

1. 拒绝同 Lease 已存在的单文件 journal，避免两套事务并存；
2. committed write-set 做幂等 replay，活动或 recovery_failed write-set 要求先恢复；
3. 重跑 Static Guard，并要求 fresh Receipt 与调用方 Receipt 完全相同；
4. 对所有文件先验证路径、symlink、baseline bytes、operation、mode 和 proposed digest；
5. 在任何文件替换前，一次持久化完整 transaction 与全部 baseline backups；
6. 按 Guard 路径顺序执行同目录 atomic replace，每成功一项立即推进 `applied_count` CAS；
7. 对全部 after digest 与完整 Git scope 做 postflight；
8. 持久化防篡改 `EvolutionPatchSetWriteReceipt`，进入 committed 并清除 backups。

`AgentEngine` 组合一个共享 `EvolutionPatchSetStore`，同时注入单文件与多文件 Writer。单文件 Writer 在锁内
反向检查 write-set，形成双向 fail-closed 互斥。

## 普通异常回滚

捕获普通 `Exception` 后，Writer 进入 `rolling_back`，从最后一个文件向第一个文件处理：

- 本轮已替换，或磁盘摘要已等于 after：恢复真实 baseline bytes/create 则删除；
- 尚未替换：仍必须确认目标等于 baseline；
- 每确认一个文件才推进逆序 rollback CAS；
- 全部确认后进入 rolled_back，清除 backups，并在抛出的 typed error 中声明
  `rollback_completed=true`；
- 任一目标无法证明、恢复失败或 journal 状态不一致：进入 recovery_failed，保留 backups，不继续猜测。

Writer 故意不捕获 `BaseException`。例如第二个文件 `os.replace` 已完成、但对应 CAS 尚未提交时，持久状态
仍为 `applying/applied_count=1`，而磁盘两个文件都是 after；2.5c2b 必须同时读 phase 和真实摘要，不能只信
游标。

## 回执

`EvolutionPatchSetWriteReceipt` 绑定 transaction、Contract、Lease、Snapshot、Plan、Guard、全部有序 change
facts 和完整 Git status digest。ID/摘要覆盖全部公开字段；篡改任一 postflight 字段都会验证失败。成功回执
可从 committed transaction 幂等读取，但每次 replay 仍重新检查磁盘 after digests 与 Git scope。

## 验收证据

- 真实双文件 authority 链路成功写入隔离 worktree，主工作树和 index 不变；
- 两文件均为 Guard after 内容，Git scope 精确等于两个路径；transaction committed、backup 已清除；
- 同一请求 replay 返回完全相同的持久回执；
- 第二次 atomic replace 抛出普通异常时，第一个文件被恢复，第二个文件确认 baseline，write-set 严格逆序
  rolled_back，Git status clean；
- 第二个 replace 后、第二次 CAS 前模拟进程崩溃时不做虚假进程内回滚：两个磁盘文件为 after，持久状态
  为 applying/applied_count=1，且可由 `scan_recoverable()` 发现；
- Write Receipt 篡改 fail-closed；Engine 同时暴露共享 store 与多文件 Writer；
- 单/多 Writer 使用同一锁路径并双向拒绝平行事务。

## 当前不足与下一切片

- `EvolutionPatchSetRecoveryCoordinator`、TUI/新 UI recovery payload、EVO-02.6b 完整 Diff/API
  Postflight 与 EVO-02.7a Mutation Receipt Core 均已由后续切片实现；
- SQLite 与文件系统依靠 intent + digest 恢复，不宣称跨介质 ACID；
- Windows 占用文件、磁盘满、断电和目录同步语义仍需平台矩阵。

EVO-02.5c2b 已复用相同锁和 write-set store，覆盖 prepared/applying/applied/rolling_back 的真实磁盘
状态组合，并把脱敏恢复结果接入 Engine、TUI 与新 terminal UI。下一步转入跨文档依赖复核。
