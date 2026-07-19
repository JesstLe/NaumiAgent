# ARC-01.4c2 New UI Terminal Lifecycle Adapter

## 1. 目标

让默认 New UI Bridge 消费 ARC-01.4c1 由 Composition Root 装配的
`TerminalRuntimeLifecycleFactory`，删除前端适配器对 heartbeat producer、retention policy 和 retention service 的
私有构造。Bridge 只负责选择 `new_ui` surface、生成实例 identity、投影 typed snapshot 和呈现中文降级通知。

本切片不迁移 Textual TUI，不建立 ARC-01.4d 全局关闭注册表，也不改变 heartbeat/retention 的持久化 schema。

## 2. 所有权与生命周期合同

- `create_agent_engine()` 构造并注入唯一 factory；Bridge 不从 `HarnessStore` 或 `AppConfig` 重建长期服务；
- `emit_ready()` 请求 factory 创建一个隔离 lifecycle，然后执行统一 `start()`；
- startup 失败返回稳定错误码并只产生一次脱敏中文降级通知，模型运行仍可继续；
- status 从 lifecycle snapshot 读取 worker 状态，从 factory 的配置深拷贝读取 `configured_enabled`，运行时修改 Engine
  config 不会让界面与实际对象图发生漂移；
- shutdown 依次执行 lifecycle draining、Engine shutdown、lifecycle terminal；Engine shutdown 失败时提交 failed
  heartbeat；draining/terminal 辅助写失败不阻断其余关闭步骤；
- 缺少 factory 的测试替身保持无副作用，不允许 Bridge 悄悄构造另一套资源。

## 3. 验收证据

- 静态检查证明 `JsonlEngineBridge` 不调用 `RuntimeHeartbeatProducer`、
  `RuntimeHeartbeatRetentionPolicy` 或 `RuntimeHeartbeatRetentionService` 构造器；
- 真实 `create_agent_engine()` 对象图经 Bridge 写入 running heartbeat，并在 graceful shutdown 后进入 stopped；
- retention 使用真实 SQLite 删除过期 stopped runtime，同时保护当前 New UI subject；
- factory 配置快照与后续 Engine config 发生漂移时，公开状态仍反映实际装配策略；
- heartbeat startup 降级、Engine shutdown failure、draining failure 和 ping 状态回归均通过；
- Ruff、compile、diff check 与 17 项 Bridge/Runtime Services 定向测试通过，未运行全量测试。

## 4. 自我审视与剩余边界

Bridge 仍保存 lifecycle 实例并编排自己与 Engine 的先后顺序，这是 frontend adapter 的合理职责；全进程资源反序关闭与
失败继续属于 ARC-01.4d，不能在本切片内伪造。Textual TUI 尚未创建 durable runtime heartbeat，因此 Doctor 仍必须显示
不可观测；下一独立 UI-17 切片应消费同一 factory，而不是复制本轮代码。完成 TUI parity 前，ARC-02 退出门仍未满足。
