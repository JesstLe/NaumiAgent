# ARC-01.4c1 Terminal Runtime Lifecycle Service

## 1. 目标

建立第一个由 Composition Root 构造、Engine 只消费的 `RuntimeServices` 切片：共享 terminal runtime lifecycle
factory。它把 heartbeat producer 与 runtime heartbeat retention 的组合规则从 New UI/TUI 适配器之外固定下来，为
ARC-02 embedded adapter 和 UI-17 parity 提供同一对象图前置。

本切片不迁移 Bridge/TUI 调用点，不建立 ARC-01.4d 的全局关闭注册表，也不把 `RuntimeServices` 冒充为完整服务包。

## 2. Composition 合同

新增：

- `RuntimeServices`：当前只包含强类型 `TerminalRuntimeLifecycleFactory`；
- `RuntimeServiceOverrides`：`None` 是唯一选择默认实现的信号；
- `build_runtime_services(config, paths, resources, overrides)`：在 Resources 之后、Engine 之前同步构造，不启动任务；
- `create_agent_engine()`：按 Paths → Ports → Resources → Services → Engine 顺序传入同一 factory；
- `AgentEngine.terminal_runtime_lifecycle_factory`：保持注入 identity，不在 Engine 内重建。

Factory 只保存规范 workspace、Composition Root 的 exact `HarnessStore` 与 retention 配置深拷贝。调用方后续修改
`AppConfig` 不会静默改变已装配运行时；构造阶段不创建数据库、目录、task 或 heartbeat 行。

## 3. 共享生命周期状态机

`TerminalRuntimeLifecycle` 采用 `created → starting → running → draining → stopped/failed`：

- `start()` 先持久化 starting/running heartbeat，再启动可选 retention；
- retention 启动失败时回滚 heartbeat 到 draining/stopped，不能遗留幽灵 runtime；
- `begin_draining()` 先停止 retention，再写 draining；retention stop 失败被记录为稳定错误码，但不阻断 terminal；
- draining 写失败后仍允许 `close(failed=True)` 提交 failed heartbeat；
- graceful/failed terminal 均幂等，重复关闭不重复写入；
- New UI 与 TUI identity 彼此隔离，当前 subject 始终进入 retention 保护集合；
- snapshot 只含 surface、subject、typed 状态、heartbeat phase/错误码与 bounded retention snapshot，不含异常正文。

生命周期不调用 Engine、不读全局 config、不发 UI event，也不决定用户文案。heartbeat 写失败通知通过显式 callback
注入，具体展示属于下一适配切片。

## 4. 验收证据

- 默认 Service 使用 exact `RuntimeResources.harness_store`、规范 workspace 与配置快照，且不产生文件；
- override identity 从 root 原样进入 Engine，非法 bundle/override 在默认构造前失败；
- 真实 Harness SQLite 完成 TUI start/running/draining/stopped 四阶段并跨 Store 可读；
- retention stop 失败仍完成 draining/stopped，公开 snapshot 不含 raw error；
- retention start 失败回滚已启动 heartbeat；
- draining 写失败后 failed terminal 仍可提交且重复 close 幂等；
- 38 项 Runtime Service、Runtime Composition、Engine bundle 与 Composition 静态节点通过；已知 legacy
  构造预算节点单独保留失败证据并在本轮定向组合中 deselect，未运行全量测试。

## 5. 已知架构门与下一步

现有 `test_legacy_test_constructor_budget_never_exceeds_arc_01_4a_baseline` 仍失败：文档阈值为 171，当前仓库实际为
204 个未显式传 `ports` 的测试构造点。本切片没有新增 `AgentEngine(...)` 调用，也没有放宽阈值；ARC-01.4d 必须
真正迁移这些构造点后恢复单调门，不能把 204 改成新基线。

下一切片 ARC-01.4c2 让 New UI Bridge 先消费共享 factory，删除 UI 内 producer/retention 构造；随后以独立 UI-17
切片让 TUI 托管同一 lifecycle。完成两端迁移前，ARC-02 仍不得开始。
