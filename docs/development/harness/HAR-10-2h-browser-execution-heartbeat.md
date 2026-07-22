# HAR-10.2h Browser Execution Heartbeat Producer

## 目标

让 `TaskRunner` 中每个真正开始执行的浏览器任务成为 HAR-10.2 typed heartbeat 的生产者。浏览器任务此前虽然
持久化了 queued/running/waiting/manual/terminal 状态，但这些字段只能说明 TaskRunner 最后写入了什么，不能证明
对应 worker 当前仍存活，也无法区分“正常等待用户”“进程失联”和“重启后遗留执行”。本切片复用 Harness Store，
不创建浏览器私有心跳表，也不把 heartbeat 冒充执行授权。

## Authority 与 identity

- `BrowserExecutionHeartbeatFactory` 由 runtime composition root 创建，与 terminal/agent producer 共享同一个
  `HarnessStore` 和规范 workspace；
- subject kind 固定为 `browser`；subject ID 由规范 workspace 与持久 browser run ID 计算 SHA-256，不包含任务正文、
  URL、Cookie、CDP endpoint 或浏览器 profile 路径；
- 每次同一 run 的恢复/重试读取上一 heartbeat 并分配 `epoch + 1`，实例 ID 每次随机生成，sequence 在 epoch 内单调；
- queued run 不创建 heartbeat。只有 TaskRunner 获得执行槽并进入真实 `_execute_run()` 后才发布 starting/running，避免
  “排队即存活”的虚假健康状态；
- heartbeat 只证明观测状态。它不授予 RunLease、浏览器控制权、恢复权、结果提交权或 destructive action 权限。

## 生命周期语义

共享 `RuntimeHeartbeatProducer` 新增保持阶段的 waiting 支持：

1. 启动：`starting/browser_starting → running/browser_running`；
2. 执行：周期 `running/browser_alive`；
3. 等待普通指令：`waiting/browser_waiting_instruction`；
4. 人工接管：`waiting/browser_manual_control`；
5. waiting 周期存活：`waiting/browser_waiting_alive`，不会错误退回 running；
6. 用户回复或结束接管：`running/browser_resumed`；
7. 收尾：`draining/browser_draining`；
8. 成功：`stopped/browser_completed`；
9. 用户取消：`stopped/browser_aborted`；
10. 运行失败：`failed/browser_failed`；
11. 重启遗留执行：新 epoch 写 `failed/browser_runtime_interrupted`。

`HarnessHeartbeatPhase.WAITING` 在 timeout 内仍由统一健康分类器判为 healthy。等待用户时 producer 继续 pulse，但不会
消耗模型轮次，也不会让 UI 把等待误画成工具正在运行。

## TaskRunner 接入

TaskRunner 持久 run 新增四个有界字段：

- `heartbeatSubjectId`
- `heartbeatEpoch`
- `heartbeatPhase`
- `heartbeatFailureCode`

生命周期对象只保存在当前进程内；持久字段是用户可见投影，不是第二权威。`onNeedsInput` 在展示问题前写 waiting，
`resume_run()` 在释放 reply future 前写 running，最终 runtime cleanup 后写 heartbeat terminal。多个并发 run 各自持有
独立 lifecycle，attached browser 的既有串行规则不受影响。

启动加载到 `starting/running/aborting/waiting/manual` 的旧 run 时，TaskRunner 先按既有规则将业务状态标为 failed，
随后用相同稳定 subject 的新 epoch 写 `browser_runtime_interrupted`。若构造时没有运行中的 event loop，则持久字段显示
`browser_heartbeat_recovery_pending`，下一次异步 queue processing 再执行 reconciliation；不会在同步构造器里偷偷启动
事件循环。

## 降级规则

Heartbeat 是诊断能力，任何 heartbeat 故障都不得改变浏览器结果：

- 查询现有 epoch 失败：`browser_heartbeat_create_failed`；
- starting/running 写入失败：`browser_heartbeat_start_failed`；
- waiting 写入失败：`browser_heartbeat_waiting_failed`；
- resume 写入失败：`browser_heartbeat_resume_failed`；
- 周期 pulse 失败：`heartbeat_write_failed`；
- terminal 写入失败：`browser_heartbeat_terminal_failed`；
- restart reconciliation 失败：`browser_heartbeat_recovery_failed`。

错误码固定且无 secret；数据库路径、URL、profile 路径和底层异常正文不会进入 run payload。周期 pulse 失败时 producer
停止刷新，最后一条 durable heartbeat 会自然进入 stale/offline，TaskRunner 同时发出 `run_heartbeat_degraded` 更新。

## UI 投影

共享 Task Panel 的 `BrowserTaskStatus` 暴露 subject、epoch、phase 和 failure code。New UI 的 typed task snapshot 与 Textual
fallback 的 `/tasks` 都从同一 Python 快照读取：列表显示 `heartbeat=<phase>@<epoch>`，详情明确显示阶段和降级码；颜色只作
辅助，文本始终保留。顺带修正了真实 TaskRunner 使用 `taskInstruction` 而旧投影只读取 `instruction` 导致标题为空的问题。

## 验收证据

- 真实 SQLite 写入 running、waiting pulse、resume、draining 与 stopped/failed，并可重新打开读取；
- instruction waiting 与 manual control 使用不同有限 detail code，waiting pulse 不篡改 phase；
- completed、aborted、failed 和 restart interrupted 均形成确定终态；
- 同一 run 重启使用递增 epoch，不同并发 run 使用不同 subject；
- heartbeat Store outage 下浏览器结果仍为 completed，且 payload 不泄漏异常路径；
- RuntimeServices/Engine 使用同一个 composition-owned factory，TaskRunner 不自行打开第二个 Harness Store；
- Task Panel 列表、详情和 typed protocol 均含 heartbeat 阶段/降级；
- 相关 TaskRunner、heartbeat、runtime services 与 task panel 小模块测试通过，不运行全量测试。

## 当前边界

本切片没有实现 browser RunLease/fencing、跨进程取消传播、浏览器外部副作用 idempotency、页面状态 reconcile、自动接管、
Supervisor restart、跨 kind catalog/history 或 heartbeat retention。SIGKILL 后由最后一条 heartbeat 先进入 stale/offline，
新进程只会把已识别的遗留 TaskRunner run 明确终结为 interrupted，不会自动继续浏览器动作。下一切片应重新对照
Harness、CLI/TUI、Claude Code source、Future Architecture 和自进化文档，选择能解锁用户闭环的最小共同前置。
