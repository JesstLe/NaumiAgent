# EVO-02.2a Experiment Worktree Lease v1

## 目标

为不可执行的 `EvolutionExperimentContract` 分配一个持久、唯一、可恢复的 Git worktree。Lease
只证明隔离目录已经按 Contract 的精确 baseline 建立；它不授予变异、命令执行、网络或依赖安装权限，
因此所有状态下 `execution_ready=false`。

本切片复用现有 `WorktreeManager` 负责 Git 生命周期，不复制第二套 worktree 实现。SQLite Lease Store
负责 Contract 绑定、并发仲裁、状态转换与崩溃恢复，两者共同形成可审计的隔离边界。

## 持久绑定

每个 Lease 固化以下事实：

- 完整 `manifest_sha256` 与其派生的 `contract_id`、`lease_id`；
- session、mission、task 与 owner；
- 精确 baseline commit、确定性 branch、worktree name 和绝对路径；
- 创建、更新、过期时间，终态原因与 cleanup 尝试次数；
- `worktree_ready` 只在 `active` 为真，`execution_ready` 永远为假。

同一 Contract 的重复获取是幂等的；不同 owner 或任何 provenance/baseline/path 变化都 fail-closed。
SQLite 使用 `BEGIN IMMEDIATE` 和条件更新进行并发仲裁，数据库层同时对 Contract、目录、branch 和路径
施加唯一约束。同一进程按 Contract 串行化 provision；另一进程遇到尚未完成的 reservation 时只能恢复
已经落盘且绑定匹配的 worktree，否则返回可重试冲突，不能抢建或提前释放。

## 生命周期

| 状态 | 含义 | 可执行 |
| --- | --- | --- |
| `provisioning` | 已预留唯一 Lease，Git worktree 尚未确认 | 否 |
| `active` | Worktree 记录、路径、baseline 和 Contract metadata 全部匹配 | 否 |
| `released` | owner 已释放，等待安全清理 | 否 |
| `expired` | 超过 Contract 时长和清理宽限，等待安全清理 | 否 |
| `tombstoned` | 目录丢失、绑定异常或存在未审查改动，需要人工复核 | 否 |
| `cleaned` | 干净 worktree 已删除或确认不存在 | 否 |

状态转换采用 compare-and-swap；重复释放和重复 reconcile 不会重新创建、强删或改变既有终态。

## Git 与恢复边界

- 先持久预留 Lease，再创建 worktree，防止并发请求各自创建目录。
- `WorktreeManager.create_from_ref()` 只接受完整 40/64 位 object ID，并确认它属于当前仓库。
- Worktree 从 Contract baseline 创建，不读取主工作树的 dirty 内容，也不覆盖 index。
- Git 已创建但进程在激活前崩溃时，reconcile 会用完整 metadata 恢复为 `active`。
- `active` 目录丢失、记录或 metadata 不匹配时立即进入 `tombstoned`，不继续宣称 ready。
- clean 且没有领先提交时允许正常删除；dirty/ahead/kept worktree 必须保留并写入 tombstone。
- 不使用 force 删除，也不清理不属于该 Lease 的目录或 branch。

## 过期策略

默认 Lease 时长是 Contract `max_duration_seconds + 300` 秒清理宽限；调用者只能缩短，不能超过
该上限，且最短 60 秒。后台或启动恢复可调用 `reconcile()`：恢复 crash window、校验 active binding、
过期 Lease，并仅清理机械确认安全的 worktree。

## 引擎组合

`AgentEngine` 现在组合并公开：

- `evolution_experiment_contract_issuer`；
- `evolution_experiment_lease_store`；
- `evolution_experiment_lease_manager`。

Lease Store 与当前会话/Workbench 使用同一 runtime SQLite 路径；Lease Manager 复用引擎唯一的
`worktree_manager`。当前没有向模型注册工具或用户命令，避免在 source snapshot/static guard 完成前
产生执行入口。

## 验收证据

- 真实 Git 仓库、Candidate SQLite、Workbench Proposal approve、Contract issuer 到 Lease acquire
  的端到端链路通过；
- 主工作树有未提交修改时，隔离 worktree 仍是 Contract baseline，主文件字节和 diff 完全不变；
- 8 路并发 acquire 只产生一个 Lease 和一个 worktree，不同 owner 被拒绝；
- clean release 可重复且安全删除；dirty release 保留目录、标记 `kept` 并写入 tombstone；
- provision 后、activate 前的崩溃窗口可恢复，过期 clean worktree 可回收；
- active worktree 在外部消失后 reconcile 立即 tombstone，不保留虚假 ready 状态；
- 超预算 Lease duration 在写数据库或创建 Git 资源前被拒绝；
- 既有 WorktreeManager 聚焦回归保持通过。

## 明确未包含

- EVO-02.4 mutation planner、02.5 patch writer、02.6 完整 static guard、02.7 receipt；
- 用户可见的实验创建按钮、斜杠命令或 Agent Tool；
- EVO-03 检查执行、before/after 评测与推广资格。

EVO-02.3a 已实现 Contract baseline tree、Harness Profile、实验配置和允许工具身份的不可变 Source
Snapshot，详见 `EVO-02-3a-source-snapshot.md`。下一切片应实现不可执行 Mutation Plan，仍不得提前开放
patch 写入。
