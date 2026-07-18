# ARC-01.4b2f Worker Registry Resource Ownership

## 目标

为 ARC-04.1b 提供唯一 Composition Root 所有权。`WorkerRegistryStore` 是 Runtime 的 worker incarnation
权威状态，不能由 daemon、Engine、Harness 或 UI 根据环境自行构造。

## 合同

- `RuntimePaths.worker_registry_db_path` 是规范绝对路径，固定在 `runtime_data_dir` 内；
- `RuntimeResources.worker_registry_store` 必须是具体 `WorkerRegistryStore`；
- `RuntimeResourceOverrides.worker_registry_store` 仅以 `None` 请求默认实例，显式 falsey 实例也必须保留；
- 默认构造只记录路径，不创建目录、DB、连接或后台任务；
- Store Catalog 使用同一个路径规则登记 `runtime.worker_registry`，不建立第二套 resolver。
- ARC-01 ownership source 将 `naumi_agent.daemons` 唯一归属 Runtime；全仓 ownership artifact 仍受此前
  `claude_source/evolution` 未登记模块阻断，本切片不伪造一份不完整 artifact。

## 验收

- 路径逃逸在默认资源构造前拒绝；
- 默认 Store path 与 `RuntimePaths` 对象一致；
- override identity 被保留，不完整 Resource bundle 构造失败；
- Runtime Composition 与 Store Catalog 均验证惰性、绝对路径和 schema v1；
- Engine 仅持有完整 Resource bundle，本切片不让 Engine 直接注册或撤销 worker。

## 未完成

该资源尚未由 Runtime Service lifecycle 启停，register/revoke 调用者也没有本机 transport 身份认证。
这些属于 ARC-02 与 ARC-04 后续；本切片只解决状态对象所有权和路径一致性。
