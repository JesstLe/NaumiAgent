# ARC-01.4b2a Harness Resource Ownership

## 目标

在 ARC-01.4b1 已提供规范 `RuntimePaths` 后，把 `HarnessStore` 与 `HarnessTrustStore` 的默认构造权
从 `AgentEngine` 移到唯一 Composition Root。这是 `RuntimeResources` 的首个真实纵向切片，直接服务于：

- HAR-06/HAR-08 的运行、Replay、Eval、Retention 和 Reconciliation；
- ARC-02 embedded/service 两种模式共享同一 Harness 状态对象；
- ARC-04 worker 未来只接收受控状态/审计端口，而不自行猜测数据库位置。

本切片不迁移 HarnessService（属于 ARC-01.4c Service ownership），不迁移其他 Store/Runner/Browser，
也不提前实现 RuntimeLifecycle。

## 类型与构造合同

`runtime/resources.py` 定义：

- frozen `RuntimeResources(harness_store, harness_trust_store)`；
- frozen `RuntimeResourceOverrides`，两个字段均以 `None` 表示使用默认实例；
- `validate_runtime_resource_overrides()`，在创建任何默认 Store 前校验全部显式 override。

Resource bundle 可以引用具体 Store 类型，因为它属于启动层对象所有权，而非领域行为 Port。显式
override 保持 identity；即使对象实现 `__bool__ -> False` 也不能被默认实例替换。

Composition Root 的当前顺序变为：

1. 构造 `RuntimePaths`；
2. 构造 `RuntimePorts`；
3. 构造 `RuntimeResources`；
4. 将三者作为同一对象图传给 `AgentEngine`。

`HarnessStore` 与 `HarnessTrustStore` 的构造函数仅记录规范路径，不打开 SQLite、不创建目录，因此本阶段
没有构造失败回滚义务。真正带连接/后台任务的资源必须等待 ARC-01.4d `RuntimeLifecycle` 或在对应后续
切片中显式提供失败清理。

## Engine 消费

Engine 保存同一个 `RuntimeResources` 实例，并将：

- `resources.harness_store` 传给 HarnessService、SessionReconciliationCoordinator 和 Retention lease；
- `resources.harness_trust_store` 传给 HarnessService；
- 不再导入或调用 `HarnessStore(...)`、`HarnessTrustStore(...)`。

legacy `AgentEngine(config, ...)` 仍会委托 Composition builder 创建完整资源包；生产入口则由
`create_agent_engine()` 显式传入 `ports + paths + resources`。该兼容入口由 ARC-01.5 后续移除。

## 验收标准与证据

- 默认资源严格使用 RuntimePaths 中的用户状态数据库路径，构造阶段不创建目录或 DB；
- 两个 override identity 均被保留，包含 falsey HarnessStore；
- 无效 override 在任一默认构造器调用前给出中文字段错误；
- 不完整 RuntimeResources bundle 构造失败；
- 生产源码中两个 Harness Store 各只有 Composition Root 一个构造点；
- AgentEngine 不导入、构造 Harness Store，生产 AgentEngine 调用显式包含 `resources`；
- 真实 streaming Engine 完成文件工具调用、session/receipt 持久化，并由注入的同一 HarnessStore 产生
  SQLite 数据库；
- 只运行 Runtime Composition、架构 AST gate、真实 streaming 与文档治理小模块，不运行全量测试。

## 自我审视与未完成

本实现是真实对象所有权迁移，不是空 Resource 容器：Harness 的实际 Service、reconciliation、retention
均消费注入实例，静态门也阻止 Engine 重新构造默认 Store。

仍未完成：

- ARC-01.4b2b 已完成 EvolutionCandidateStore；ChatRunStore、Task/Workbench、Background/Scheduler、
  Goal/Pursuit 仍未迁移；
- BrowserRuntime、BrowserDaemonClient、WorktreeManager 的资源和失败清理；
- HarnessService 等长期 Service 的 Composition Root 装配；
- reverse-order、幂等、failure-isolated RuntimeLifecycle；
- ARC-01.5 legacy adapter 删除与 ARC-01.6 import rule。

因此 ARC-01.4 继续保持 partial，下一步应优先迁移无后台任务的 Store，不能直接把 Browser/Runner 一次
塞入 Resource bundle 而跳过生命周期设计。
