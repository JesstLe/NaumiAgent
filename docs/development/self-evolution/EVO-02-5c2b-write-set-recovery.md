# EVO-02.5c2b Multi-file Write-set Crash Recovery v1

## 目标

在 runtime 启动后台服务前，机械恢复 EVO-02.5c2a 遗留的多文件 write-set。恢复必须读取持久 transaction、
全部 backup 和真实文件摘要，在任何不确定状态下 fail-closed；不得依赖模型推理、交互 permission 或 bypass。

本切片把多文件恢复结果合并到既有 TUI 与新 terminal UI 的“实验补丁恢复”状态，不创建平行 UI 协议。

## 启动顺序

`AgentEngine.start_long_running_services()` 依次执行：

1. `EvolutionPatchSetRecoveryCoordinator.recover_pending()`；
2. 既有单文件 `EvolutionPatchRecoveryCoordinator.recover_pending()`；
3. Session reconciliation；
4. 周期 retention worker。

多文件恢复失败会像单文件恢复失败一样阻止后续后台服务隐式启动。单文件 orphan-lock 扫描同时读取活动
write-set Lease，不能把活跃或待恢复的多文件锁误删为孤儿锁。

## 恢复证据与判定

Coordinator 只扫描 `prepared/applying/applied/rolling_back` transaction，并在共享 Lease 锁内重新读取当前
SQLite 状态。开始触碰文件前必须一次性完成：

- transaction/file fact/row binding/backup BLOB 摘要验证；
- worktree name 与绝对路径绑定；
- 所有目标路径、父目录与 symlink 边界验证；
- 单文件 journal 冲突检查；
- 每个目标分类为 `before`、`after` 或 `unknown`。

任一目标为 `unknown` 时，整组进入 `recovery_failed/target_digest_unknown`，不恢复任何其他文件并保留全部
backups。损坏 transaction/backup 转为脱敏 `patch_set_corrupt` 或 `recovery_evidence_invalid`，不虚构源码、
文件数量或成功结果。

## 严格逆序恢复

- 非 rolling-back 状态先 CAS 到 `rolling_back`，cursor 从最后一个文件开始；
- 已是 rolling-back 时，cursor 后的所有文件必须真实等于 before，否则
  `rollback_progress_mismatch`；
- 从 cursor 向 0 遍历：after 则恢复 backup，create 则删除；before 则只验证；
- 每个文件验证 baseline 后才推进 `mark_file_rolled_back(index)`；
- cursor 到 -1 后才进入 `rolled_back` 并清除 backups；
- 全组原本都是 before，结果为 `already_baseline/filesystem_changed=false`；
- 至少恢复一个 after，结果为 `rolled_back/filesystem_changed=true`。

锁属于活进程或异机 owner 时结果为 `deferred`，不触碰 lock、target 或 journal。失败 transaction 保留
backup，等待人工检查或后续受控恢复。

## TUI 与新 UI

Engine 继续输出统一 `evolution_patch_recovery` 摘要，并新增：

- `single_file_total`：本轮单文件 journal 数；
- `multi_file_total`：本轮多文件 transaction 数。

协议校验要求二者之和严格等于 `total`。旧 payload 缺少新字段时按“全部为单文件”兼容；新 runtime 始终
提供明确分类。TUI 状态栏在多文件事务非零时显示“多文件 N”；新 UI 恢复卡片显示“多文件事务 N”，并沿用
info/warning/error 级别、完成/失败/延后、filesystem changed 与脱敏 failure codes。

## 验收证据

- 真实 Git worktree + SQLite 覆盖 prepared、applying/CAS 窗口、rolling_back 中断三个状态；
- prepared 全 baseline 不写文件并终结为 already_baseline；
- 两文件已 after、但只提交第一个 CAS 时，恢复器按逆序恢复两文件并得到 clean Git status；
- rolling_back 已恢复尾文件时，只从当前 cursor 继续，不重复或跳序；
- 第三种摘要使整组 fail-closed，未知文件与其余 baseline 均不被改写，backups 保留；
- Engine 证明多文件恢复先于单文件、Session 和 retention；
- Engine 汇总严格区分 single/multi，协议拒绝分类计数不一致；
- TUI 与新 UI 聚焦测试显示多文件恢复数量且不暴露 backup/source 内容。

## 当前不足与下一步

- SQLite 与文件系统仍通过 durable intent + digest 达成可恢复语义，不是跨介质 ACID；
- Windows 文件占用、断电、磁盘满、目录 fsync 差异仍需 A4/A5 平台矩阵；
- recovery_failed 的人工审查/重试操作面尚未进入 Workbench；
- 当前 postflight 是全文件 after digest + 精确 Git scope，尚缺 EVO-02.6b 完整 diff/API surface policy；
- EVO-02.7a Receipt Core 与 2.7b1 Generation Trace 已在后续切片实现；2.7b2 Trace→Receipt v2 绑定和
  HAR-08 RED/GREEN 尚未串成最终实验闭环。

完成本切片后，EVO-02.5c 的多文件写入与启动恢复链路闭合。下一开发选择应重新横向检查 Harness、CLI/TUI、
Future Architecture 与自进化依赖，优先推进能解锁 HAR-08/EVO-03 的最小前置，而不是继续无限扩张 Writer。
