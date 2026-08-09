# HAR-10.2k Release-bound Chat Run Provenance

## 目标

把默认 New UI 与 Textual TUI 中真正开始的每个 managed chat run，绑定到启动该运行的 exact
`HarnessRuntimeReleaseBinding`。该切片只建立不可变来源事实，解决“这次用户任务究竟由哪个已安装发布版本执行”这一问题，
不把运行开始、心跳存活或普通完成回执提升为自进化阶段完成证据。

## 实现

- `TerminalRuntimeLifecycle` 暴露启动时原子写入的 release binding，并通过 `ContextVar` 为单个异步任务建立
  task-local scope；并发 New UI/TUI 运行不会串用 binding，scope 退出后不会污染后续 API 或 legacy 调用。
- New UI Bridge 的普通消息与 Workbench task、Textual TUI 的真实 `run_streaming` 入口均在 lifecycle scope 内调用
  `AgentEngine`；未启动、已关闭、unmanaged 或 binding 失败的 lifecycle 只产生无 authority 的普通运行记录。
- `AgentEngine` 在落库前重新读取 Harness Store，要求 workspace、subject 和完整 binding 与当前持久事实完全一致。
  读取失败或 binding 已变化时不阻断用户任务，而是 fail closed 地省略 release provenance。
- `RunReleaseProvenance` 使用 canonical JSON 和 SHA-256 生成 content-addressed identity，冻结 workspace、run ID、
  surface、exact runtime identity/binding 与运行开始时间，并固定声明 completion、execution outcome、stage completion
  authority 均为 false。
- `ChatRunStore.start_run()` 在创建运行的同一个 SQLite 事务中写入 provenance ID 与完整 JSON；旧数据库自动增加两列，
  payload 损坏或摘要不一致时读取降级为无 provenance，绝不返回半可信对象。
- `ChatRunRecorder` 在公共边界再次校验 binding workspace，阻止绕过 Engine 后把其他工作区的合法 binding 错绑进当前运行。

## 权限边界

本切片可以证明：

1. managed terminal lifecycle 启动时绑定了哪个精确发布版本；
2. 哪个 chat run 在该 lifecycle 的 task-local 上下文中开始；
3. 持久 binding 在运行开始时仍与 Harness Store 一致。

本切片不能证明：

- 模型响应、工具调用或用户目标成功；
- heartbeat 在整个运行区间连续存活；
- completion receipt 未被替换或已满足自进化 outcome policy；
- `minimum_completed_runs` 或任何 opt-in stage completion 门槛已经满足。

## 验收

- New UI/TUI lifecycle 的并发 scope 返回各自 exact binding，退出后上下文为空；
- managed Engine run 持久化 exact provenance，未绑定 Engine run 明确保持 `None`；
- SQLite 重启恢复后 provenance 完全一致，legacy 表迁移不丢数据；
- 摘要篡改、伪造 execution authority 和跨 workspace 绑定均被拒绝或 fail closed；
- ruff、compile 与 `test_harness_runtime_release_binding.py`、`test_chat_runs.py`、
  `test_engine_event_pipeline.py` 小模块测试通过；不运行全量测试。

## 下一步

下一最小 EVO 切片建立 release-bound execution outcome ledger：只消费本 provenance、同一 `ChatRunStore` 中的真实终态
`CompletionReceipt`，以及覆盖运行起止区间的 HAR observation evidence，生成独立 content-addressed outcome。只有 outcome
达到 policy 要求后，后续聚合器才可以计算 `minimum_completed_runs`，本 provenance 本身永远不能直接成为 Stage Completion
Evidence。
