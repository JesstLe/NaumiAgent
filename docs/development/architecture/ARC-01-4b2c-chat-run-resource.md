# ARC-01.4b2c ChatRun Resource Ownership

## 目标

把承载 streamed Agent run、step、artifact、source 与 completion receipt 的 `ChatRunStore` 从 Engine
默认构造迁入 Composition Root。该资源同时服务新 UI/TUI history、Runtime Inspector、API Chat
Environment，并是 ARC-02 event cursor/resume 的持久化前置。

本切片不把当前 ChatRun schema 冒充 ARC-02 Event Store：cursor、revision、ack、bounded buffer 与
slow-client recovery 仍未实现。

## 路径与资源合同

- `RuntimePaths.chat_run_db_path` 固定为规范 `runtime_data_dir/chat-runs.db`；
- 路径必须是 absolute/canonical，且不能逃逸 `runtime_data_dir`；
- `RuntimeResources.chat_run_store` 与对应 override 使用具体 `ChatRunStore` 类型；
- 所有 override 在任一默认资源创建前统一验证，`None` 是唯一默认信号；
- Engine、API、Bridge、Inspector、Chat Environment 继续通过 `engine.chat_run_store` 消费同一注入实例。

`ChatRunStore` 没有常驻 SQLite connection 或后台任务，每次操作使用独立连接；构造时会幂等创建
数据库父目录。Composition Root 将它放在当前默认资源构造序列末端，因此构造之后没有会导致半对象图的
默认 Resource 步骤。未来新增带失败可能的资源时，必须先进入 ARC-01.4d RuntimeLifecycle/rollback，
不能依赖此顺序长期规避清理。

## 架构门

1. Engine 不导入或调用 `ChatRunStore(...)`。
2. 生产源码唯一构造点是 `runtime/composition.py`。
3. `create_agent_engine()` 将同一 `RuntimeResources` 交给 Engine；API lifespan 再暴露其中相同实例，
   不重开数据库。
4. 显式 override 保持 identity；无效/不完整 bundle 在 Engine 创建前失败。

## 验收证据

- Runtime Composition 单测覆盖默认路径、override identity、escape 与 bundle 完整性；
- AST gate 覆盖唯一构造点和 Engine import/call 禁令；
- 真实 Composition Engine 通过同一 Store 完成 run start、工具事件、terminal receipt 和 history 恢复；
- durable event pipeline 覆盖成功、异常、取消及 terminal sink 失败，receipt 在发送终态前已落库；
- ChatRun Store 小模块覆盖 step 幂等、artifact/source、重开恢复、旧 schema 兼容与 session 隔离；
- 文档治理通过，不运行全量测试。

## 用户价值与未完成

此次迁移保证默认新 UI、TUI fallback、API 与 Inspector 不会因为各自构造 Store 而看到不同 history；
Runtime 服务化后也有明确的持久化 owner。

仍未完成：

- ARC-02.5 cursor/revision/ack/resume 与 slow-client snapshot recovery；
- UI-11 大 Timeline viewport/筛选/恢复体验；
- ChatRun schema 纳入 ARC-05 migration catalog 的完整一致性审计；
- Task/Workbench、Goal/Pursuit、Background/Scheduler、Browser/Worktree 资源迁移；
- ARC-01.4d 统一生命周期与 ARC-01.4c Service ownership。

因此 ARC-01、ARC-02 与 UI-11 均保持 partial/planned 的既有状态。
