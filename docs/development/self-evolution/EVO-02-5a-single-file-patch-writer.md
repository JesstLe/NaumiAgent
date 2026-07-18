# EVO-02.5a 单文件原子 Patch Writer v1

## 目标与边界

把 EVO-02.6a 已审查的一个具体文件内容安全写入 Contract 专属隔离 worktree。Writer 不调用模型、
不运行验证命令、不写主工作树、不提交 Git，也不授予后续执行或推广权限。

本切片有意只支持单文件。单个同目录 `os.replace` 可以提供清晰的原子替换语义；多文件若没有持久
intent journal，只能做到进程内尽力回滚，不能诚实宣称崩溃原子性。持久恢复由 EVO-02.5b 补齐，
多文件事务留给 EVO-02.5c。

## 权威输入

`EvolutionPatchWriter.apply()` 同时接收：

- `EvolutionExperimentContract`、active `ExperimentWorktreeLease`；
- 写前 `EvolutionExperimentSourceSnapshot` 与 `EvolutionMutationPlan`；
- `preflight_passed=true` 的 `EvolutionStaticGuardReceipt`；
- 恰好一个 `path → str/bytes` 完整提议内容。

Writer 获取 Lease 专属跨进程排他锁后，重新执行 Static Guard preflight。fresh Receipt 必须与调用者提交的
Receipt 完全相同，提议内容 SHA-256 也必须等于唯一 change fact，才会进入写入阶段。bypass 不参与该链路。

## 写入事务

1. 锁文件使用 `O_CREAT | O_EXCL`，位于受管 worktree 存储父目录，不污染 Git 工作树；
2. 写前重新检查目标及现存父级的 symlink、worktree containment、baseline 类型与摘要；
3. 临时文件在目标同目录以 `O_EXCL` 创建，循环处理 short write，`fsync` 后再替换；
4. 修改文件保留普通 `0o777` 权限位但剥离 setuid/setgid/sticky，新文件使用 `0o644`；
5. `os.replace` 后在 Unix fsync 父目录；Windows 跳过不支持的目录 fsync；
6. postflight 重新读取普通文件摘要，并要求 Git porcelain 状态只包含该 Guard change；
7. postflight 失败时，以同一原子替换路径恢复原字节；create 则删除新文件并同步父目录。

任何异常都只返回 typed `EvolutionPatchWriteError.code` 与 `rollback_completed`，不把提议正文带入错误。

## 写入回执

成功产生 `EvolutionPatchWriteReceipt`，包含 Contract/Lease/Snapshot/Plan/Guard provenance、唯一 change fact、
Git status 摘要、postflight 状态和 canonical `write_sha256`。回执固定：

- `postflight_passed=true`；
- `rollback_performed=false`；
- `write_completed=true`；
- `execution_ready=false`。

修改回执字段而沿用旧摘要会在 Pydantic 反序列化阶段被拒绝。它只是 EVO-02.5 写入事实，不替代包含
验证证据、rationale 与 attempt 的 EVO-02.7 Mutation Receipt。

## 验收证据

- 真实 Candidate→Contract→Lease→Snapshot→Plan→Guard→Writer 链路只修改隔离 worktree；主工作树和
  index 原字节不变；
- 不同正文复用旧 Guard Receipt 在写前被拒绝；
- 两个并发 replay 至多一个成功，另一请求被锁或 fresh Guard 拒绝；
- postflight 故障注入发生在真实 `os.replace` 之后，原文件字节与 clean Git 状态被恢复；
- `os.replace` 已成功但第一次目录 `fsync` 失败时，以目标摘要识别已发生替换并完成第二次原子回滚；
- 成功回执字段篡改被摘要校验拒绝；Engine 只组合一个 Guard 与一个 Writer 实例。

## 当前不足与后续

- EVO-02.5b 已补持久 intent journal、owner lock、prepared/replaced 崩溃恢复、孤儿锁清理与启动 UI 状态，
  详见 `EVO-02-5b-patch-journal-recovery.md`；
- 多文件事务、完整 Diff/API Postflight 与 EVO-02.7a Mutation Receipt Core 已在后续切片实现；
  EVO-02.7b1 mutation-generation trace 已实现；2.7b2 Trace→Receipt v2 绑定与 HAR-08 RED/GREEN
  尚未串联；
- Windows 的文件占用可能让 `os.replace` 明确失败；当前不降级为非原子覆盖，后续平台测试需覆盖
  Defender、长路径和占用文件场景。

下一切片可在该 journal 状态机上设计 EVO-02.5c 多文件 write-set 与反向恢复协议。
