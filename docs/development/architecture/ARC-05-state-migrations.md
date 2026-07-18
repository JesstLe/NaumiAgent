# ARC-05 状态 Schema 与迁移平台

## 目标

统一 Session、Task、Goal/Pursuit、Harness、Workbench 和 daemon 元数据的 schema 版本、迁移、
备份、校验和恢复，避免每个 Store 自己发明升级逻辑。

## 子模块

- ARC-05.1 Store catalog：路径、owner、schema version、数据敏感级别、保留策略。
- ARC-05.2 Migration runner：前向、幂等、事务、锁、dry-run、progress、cancel 边界。
- ARC-05.3 Preflight backup：空间检查、原子 snapshot、manifest/digest、权限。
- ARC-05.4 Integrity check：foreign key、orphan、JSON/schema、artifact reference。
- ARC-05.5 Recovery：失败回滚、只读安全模式、导出诊断、人工修复入口。
- ARC-05.6 Cross-store saga：Session 删除等跨库操作用 reconciliation/tombstone。
- ARC-05.7 Retention/GC：按 owner policy、引用计数、legal hold。

## 验收标准

- 空库、当前库、每个历史 fixture、部分迁移、损坏库均有测试。
- 迁移失败后旧版本数据可恢复；不允许半版本继续写。
- 用户状态目录权限为 0700、数据库 0600（适用 POSIX）；Windows ACL 等价。
- dry-run 列出预计行数、空间、不可逆步骤，不改变任何字节。
- 跨库操作中断后可重启 reconcile，不重复删除或遗留永久孤儿。
- 发布包包含 schema compatibility matrix 和 downgrade 说明。

## 实现进度

### ARC-05.1 Store Catalog（已实现，2026-07-17）

- `src/naumi_agent/persistence/store_catalog.py` 登记 14 个物理 Store，覆盖共享
  Runtime Core、Run、Goal、Pursuit、Scheduler、Harness、Harness Trust、Background、
  Browser Runtime、Browser Daemon、Vector Memory、Evolution Candidates、Worker Registry 和
  Execution Grants。
- 每项包含稳定 ID、绝对路径、一个或多个 owner、存储类型、version strategy、支持的
  schema version、敏感级别、retention policy 和惰性创建语义。
- SQLite 使用只读 URI 读取 `PRAGMA user_version`；JSON 有 8 MiB 有界解析；目录只读探测。
  缺失 Store 不创建，高版本/损坏/类型错误明确报错，未版本化和 POSIX 权限过宽明确提醒。
- `/doctor`、TUI Doctor 与 Agent `doctor_diagnostics` 通过共享 `run_doctor()` 自动显示
  “状态存储目录”，不建立第二套 UI 或路径推导。
- 首次实现时的本机真实配置验证了原 11 项中 7 项已存在、4 项未创建、0 项错误；检查前后
  已存在文件的 size、mtime、mode、SHA-256 完全相同。新增 Evolution Store 同样采用惰性
  Catalog 探测，缺失时不创建。

### ARC-05.2a SQLite Migration Runner 内核（已实现，2026-07-18）

- `src/naumi_agent/persistence/migrations.py` 提供相邻版本步骤、连续 Registry、只读 Plan、
  typed progress/result/error 和单 Store Runner。
- dry-run 使用 SQLite 只读 URI，不创建缺失文件；报告文件大小、预计处理行数和不可逆步骤。
- apply 使用 `BEGIN EXCLUSIVE`，锁内复核版本，并将全部 DDL/DML 与 `user_version` 更新放入
  单一事务；步骤边界支持取消，重复执行当前版本不产生写入。
- SQLite authorizer 禁止迁移步骤自行 commit、隐式 transaction control 或 attach 其他库，
  防止步骤逃逸全事务回滚保证；进度回调故障不影响迁移结果。
- 真实 SQLite 测试覆盖成功、幂等、失败回滚、取消回滚、锁冲突、事务逃逸、高版本、损坏库、
  缺失文件和 Registry 断层。详细契约见 `ARC-05-2a-sqlite-migration-runner-design.md`。

尚未完成 ARC-05.2b 与 ARC-05.3-05.7；当前内核未自动接管生产 Store，也不替代升级前备份、
恢复和跨 Store 编排。Catalog 仍只报告权限问题，不自动修改用户文件。ARC-05 保持
`partial (5.1, 5.2a)`。

HAR-06.4 已在 Harness 领域内实现 Session 删除专用的 Artifact 引用安全 GC 和 v4→v5 状态迁移，
但它不替代 ARC-05.7 的跨 Store 通用 retention/GC 平台；ARC-05 状态因此仍保持
`partial (5.1, 5.2a)`。
