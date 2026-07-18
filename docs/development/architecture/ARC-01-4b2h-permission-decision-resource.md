# ARC-01.4b2h Permission Decision Resource Ownership

## 目标

让 UI-12.3a 的 `PermissionDecisionReceiptStore` 只由 Runtime Composition Root 创建和注入，禁止 Engine、UI、
execution grant authority 或 daemon 自行推导数据库位置。

## 路径与对象图

- `RuntimePaths.permission_decision_db_path` 固定为 `runtime_data_dir/permission-decisions.db`，必须是规范化绝对
  Path 且不得逃逸 runtime data directory。
- `RuntimeResources.permission_decision_store` 是完整资源 bundle 的必填成员；测试可通过
  `RuntimeResourceOverrides` 注入同类型实例，falsey 实例也按 identity 保留。
- `build_runtime_resources()` 只构造惰性 Store 对象，不建目录、不打开 SQLite。
- Engine、Permission Center 与 `ExecutionGrantAuthority` 共享同一个实例；物理 Store 同时登记到 Catalog。

## 验收

- 默认路径、escaped path、override identity、错误类型与不完整 bundle 均有定向测试；
- 构造真实 Engine 后 permission database 仍不存在；首次终态决定才物化；
- Store Catalog 与 Doctor 使用同一路径和 schema version 事实。

该切片只解决资源所有权，不让 Composition Root 承担权限判断、UI 渲染或 retention 执行。
