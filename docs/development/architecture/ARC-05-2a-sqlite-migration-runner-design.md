# ARC-05.2a SQLite Migration Runner 设计与验收

## 1. 交付边界

本切片提供单个、已登记 SQLite Store 的通用前向迁移内核，供 Harness、Task、Goal/Pursuit、
Workbench 等领域后续复用。它解决迁移计划、只读预检、排他锁、单事务执行、进度、取消与
幂等重入，不负责自动发现迁移、启动时自动升级、备份、恢复 UI 或跨 Store 编排。

本切片不会接管任何生产 Store。领域 Store 只有在声明完整的历史迁移步骤、接入 ARC-05.3
备份与相应恢复策略后，才能选择调用该内核。

## 2. 核心类型

- `MigrationStep`：绑定稳定 `store_id`，且只允许 `N -> N+1`；包含真实 SQLite 变更函数、
  用户可读说明、可选只读规模估算语句和不可逆标记。
- `MigrationRegistry`：启动时验证重复起点和版本断层，并按当前版本到目标版本生成唯一链路。
- `MigrationPlan`：报告当前/目标版本、文件大小、预计处理行数和不可逆步骤；不写文件。
- `MigrationRunner`：在 SQLite `BEGIN EXCLUSIVE` 锁内重新确认版本，并把全部步骤和
  `PRAGMA user_version` 更新放在同一事务。
- `MigrationProgress`：结构化输出 `started`、逐步 `step` 和提交后的 `completed`。
- `MigrationResult`：区分已迁移与已是当前版本，支持安全幂等重试。

## 3. 安全与一致性不变量

1. dry-run 使用 `mode=ro` 和 `query_only`，缺失文件直接失败，绝不隐式创建数据库。
2. 只支持 Store Catalog 中声明为 SQLite 且采用 `sqlite_user_version` 的定义。
3. 高于运行时支持版本的数据库拒绝降级；损坏库返回稳定中文错误，不泄露底层异常内容。
4. 写入前取得排他锁，并在锁内重读版本；预检后被其他进程改变时拒绝继续。
5. 所有步骤处于一个事务；任一步骤、取消检查或最终提交失败都会回滚整个迁移。
6. SQLite authorizer 禁止步骤自行控制事务、附加或分离数据库，阻止 `commit()` 与
   `executescript()` 的隐式提交逃逸原子边界。
7. 取消只在步骤边界生效，不会中断正在执行的 SQLite 语句并留下未知状态。
8. 进度回调是旁路观察能力；回调自身失败会记录日志，但不会改变迁移事务结果。

## 4. 错误语义

- `MigrationLockedError`：数据库被其他连接或进程占用，可稍后重试。
- `MigrationCancelledError`：在安全步骤边界收到取消，已完整回滚。
- `MigrationExecutionError`：定义不兼容、历史步骤缺失、数据库损坏或步骤执行失败。

用户可见错误只包含 Store ID、失败版本边和恢复结果；私有 SQL、异常信息与数据内容只保留在
异常因果链中供受控诊断，不拼入 UI 文案。

## 5. 已验证场景

- 0→2 真实 SQLite 历史库预检；SHA-256、字节数和 mtime 前后完全一致。
- 0→2 成功迁移、结构和索引落地、版本更新、四阶段进度与第二次幂等执行。
- 第二步抛出私有异常时，第一步 DDL、索引和版本号全部回滚，用户文案不泄密。
- 第一步后取消时全事务回滚。
- 恶意/误写步骤主动 `commit()` 时被 authorizer 拒绝且 DDL 回滚。
- 真实排他锁冲突返回 typed lock error。
- 高版本库、损坏库、缺失文件和 Registry 版本断层均明确拒绝。
- 与 ARC-05.1 Store Catalog 的聚焦回归共同通过。

## 6. 后续依赖

- ARC-05.2b：多 Store 升级编排、持久化进度和进程崩溃后的启动判定。
- ARC-05.3：升级前空间检查、原子 snapshot、digest manifest 与权限继承。
- ARC-05.4/05.5：迁移前后完整性检查、失败只读模式与恢复入口。
- HAR-08 H5：声明 Harness Eval/Baseline 历史版本步骤并复用本内核；不得复制一套迁移器。

## 7. 当前已知限制

- `MigrationStep.apply` 是随发布包提供的受信代码，不接受运行时用户 SQL。
- 单个长 SQL 运行期间不响应取消；这是保证 SQLite 原子性的有意边界。
- 当前锁和事务只覆盖单个 SQLite 文件；跨文件一致性必须由 ARC-05.6 saga 处理。
- 在 ARC-05.3 完成前不得把本内核接到无提示的启动时自动迁移路径。
