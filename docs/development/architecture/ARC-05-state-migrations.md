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
