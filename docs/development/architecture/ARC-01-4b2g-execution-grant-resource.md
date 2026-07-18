# ARC-01.4b2g Execution Grant Resource Ownership

## 目标

为 ARC-04.2a 提供唯一 Composition Root 所有权，避免 Engine、daemon 或 UI 各自推导 grant 数据库路径或创建
第二个 Store。

## 合同与验收

- `RuntimePaths.execution_grant_db_path` 固定在 `runtime_data_dir/execution-grants.db`，逃逸路径拒绝；
- `RuntimeResources.execution_grant_store` 必须是 `ExecutionGrantStore`，falsey override 仍按显式实例保留；
- `build_runtime_resources()` 只构造惰性 Store 对象，不创建文件；
- AgentEngine 从完整 Resource bundle 组合 `ExecutionGrantAuthority`，复用同一 Worker Registry、Harness Store
  和 workspace root；
- Store Catalog 同路径登记 `runtime.execution_grants` schema v1；
- 定向测试覆盖默认路径、override identity、不完整 bundle、惰性构造与 Store Catalog 一致性。

## 未完成

Runtime Service 尚未成为 execution grant 的跨进程单写者，daemon transport 也未认证。本切片只解决生产对象图
和路径所有权，不能证明 Tool daemon 已经可执行。
