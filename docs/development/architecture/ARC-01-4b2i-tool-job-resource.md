# ARC-01.4b2i ToolJob Resource Ownership

`ToolJobStore` 是 ARC-04.2b admission 与 ARC-04.2c lifecycle receipt 的 Runtime-owned 状态资源：

- `RuntimePaths.tool_job_db_path = runtime_data_dir/tool-jobs.db`；必须是规范化绝对路径且不得逃逸；
- `RuntimeResources.tool_job_store` 是完整 bundle 的必填成员，override 只按显式 `None` 选择默认值；
- `build_runtime_resources()` 只构造惰性对象，不创建目录或数据库；
- Engine 的 `ToolJobAuthority` 与 `ExecutionGrantAuthority` 共享同一 Worker Registry 和 grant authority；
- `ToolJobLifecycleAuthority` 复用同一 Store，不创建第二套 Job 身份、数据库或状态机；
- Store Catalog/Doctor 使用同一 schema/path 事实。

该资源不拥有 Worker 选择、权限判断、命令执行、transport、Supervisor reconcile 或 UI 渲染。
