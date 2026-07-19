# ARC-01.4c3 TUI Terminal Lifecycle Adapter

## 1. 目标

让 Textual TUI fallback 消费 ARC-01.4c1 的 Composition-owned
`TerminalRuntimeLifecycleFactory`，补齐 New UI 已在 ARC-01.4c2 获得的 durable runtime heartbeat、周期 retention、
Doctor 实时诊断和 terminal 收尾语义。这是 UI-17 parity 的最小运行时前置，不展开 capability manifest、golden scenarios
或发布矩阵。

## 2. 注入与状态合同

- `_launch_tui()` 把 `create_agent_engine()` 注入的 exact factory 显式传给 `NaumiApp`；TUI 不通过 Engine service
  locator 查找依赖，也不构造 producer、policy 或 retention service；
- `NaumiApp` 只请求一个隔离的 `surface=tui` lifecycle，测试替身可显式不注入，从而不产生隐藏文件或后台 task；
- async mount 在恢复其他长期服务前启动 lifecycle；失败只显示一次中文降级提示，不退出界面、不暴露异常正文；
- Doctor 从 live lifecycle snapshot 投影 configured/state/cycle/deleted/failure/error/time/delay；factory 缺失时诚实显示
  `UNKNOWN` 并提示检查安装与 Composition Root，不再宣称 TUI 永远不托管 worker；
- unmount 先 draining，再清理 TUI owner/queue、关闭 Engine，最后提交 stopped；任何中间失败都尝试提交 failed，辅助
  heartbeat 写失败不遮蔽原始关闭异常；DebugTrace 在关闭链末尾释放。

## 3. 验收证据

- 真实 `create_agent_engine()` + Textual `run_test()` + Harness SQLite 写入 TUI running heartbeat，退出后为 stopped；
- retention 启用时 Doctor 看到真实 `waiting/standby`，而不是硬编码 unavailable；
- Engine shutdown 抛错时 durable heartbeat 与 lifecycle 都进入 failed，原异常继续上抛；
- heartbeat startup Store 故障时 TUI 保持可用且降级通知只发一次；
- 启动入口测试证明传给 App 的 factory 与 `RuntimeServices` 中对象 identity 相同；
- 静态检查证明 `NaumiApp` 不构造 heartbeat/retention 具体组件；
- Ruff、compile、diff check 与 TUI/Doctor/launcher 定向测试通过，未运行全量测试。

## 4. 自我审视与后续边界

New UI 与 TUI 仍各自编排 Engine shutdown 的相对顺序，这是 adapter 层尚存的重复；ARC-01.4d 应由统一
`RuntimeLifecycle` 接管进程级反序关闭，而不是现在抽取一个只覆盖两处的半成品 helper。两端 retention status 的协议
golden fixture、capability manifest 与跨版本 negotiation 仍属于 UI-17.1-17.3。legacy 测试构造门仍为 204 > 171；本切片
没有新增直接 `AgentEngine(...)` 构造，也未放宽门槛。
