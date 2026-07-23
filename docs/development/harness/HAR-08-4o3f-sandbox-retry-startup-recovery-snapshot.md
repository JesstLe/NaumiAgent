# HAR-08.4o3f Sandbox Retry Startup Recovery Snapshot

## 状态

已实现，2026-07-24。

本切片让 New UI Bridge 与 Textual TUI 在新进程启动后发现当前工作区尚未结束的 Sandbox retry
dispatch，并把可安全操作的项目投影成有界人工恢复队列。启动扫描只读取 HAR-08.4o3d 的
`open` catalog；不会 claim ticket、续租、生成 Permission receipt、调用 resume Tool 或启动 Worker。

HAR-08.4o3e 已提供真正的 receipt-bound resume 权威。本切片只解决“新进程如何知道有恢复工作”，
不创建第二套执行状态机。

## 依赖结论

实现前横向核对：

- HAR-06 要求所有启动恢复扫描有硬上限，失败不能伪造其他项目成功；
- HAR-07/ARC-02 禁止在缺少持久事件与副作用证据时自动重放活动运行；
- HAR-08.4o3d 已经提供 workspace-scoped、完整校验、固定评估时间的 catalog；
- HAR-08.4o3e 已经提供 Permission/receipt/dispatch/ticket fence 绑定的显式 resume。

因此本切片只做“有界发现 + 人工入口”，不自动执行 catalog 中的任何项目。

## 权威快照

`HarnessService.sandbox_retry_recovery_snapshot()` 固定调用：

```text
list_sandbox_retry_dispatches(
  state_filter="open",
  limit=20
)
```

它返回 `HarnessSandboxRetryRecoverySnapshot`：

- schema version 固定为 1；
- `snapshot_id` 由 canonical snapshot SHA-256 前 24 位派生；
- `workspace_sha256` 只绑定工作区，不暴露路径；
- `assessed_at` 来自 catalog 的同一固定评估时间；
- `limit` 最大 20，`items` 不得超过 limit；
- `truncated=true` 只允许出现在填满 limit 的页面；
- counts 必须与 items 的机械分类逐项一致；
- `actionable = pending + recovery_required`；
- unavailable 快照必须为空，只携带稳定错误码，不携带异常原文。

每个 item 只携带：

- dispatch/action/retry receipt identity 与 receipt SHA；
- Batch/Suite、requested/persisted H5a；
- recovery status、dispatch epoch；
- ticket ID/epoch/state/lease；
- 更新时间；
- 是否可以显式 resume；
- 从上述持久事实机械生成的完整 resume 命令。

快照不包含 workspace 路径、owner ID、execution authority、Permission receipt、cancel receipt 内容、
原请求参数或工具输出。

## 一致性与防篡改

Python Pydantic 模型在构造与反序列化时复验：

1. pending item 必须没有 ticket/claim/epoch；
2. 非 pending item 必须来自 claimed dispatch，并携带合法 ticket fence；
3. 只有 `pending/recovery_required` 可以包含 resume 命令；
4. resume 命令必须精确绑定 item 的 action、dispatch、receipt 与 SHA；
5. H5a persisted 不得超过 requested；
6. counts、total、limit、truncated 必须一致；
7. canonical snapshot SHA 与 snapshot ID 必须一致。

New UI 不把 Bridge JSON 当成可信展示文本。JavaScript protocol normalizer 使用相同 canonical JSON
规则重新计算 SHA，并复验字段闭集、identity、时间、计数、ticket fence 与 resume 命令。未知字段、
命令替换、计数漂移或摘要变化都会拒绝整条 ready/status record。

## 启动生命周期

### New UI Bridge

`JsonlEngineBridge.emit_ready()` 顺序为：

1. 启动既有 Terminal Runtime lifecycle；
2. 在 2 秒超时和 20 项上限内读取 recovery snapshot；
3. 将快照放入同一 `ready` payload；
4. 再对外发布 ready；
5. 继续既有 heartbeat 降级、durable interaction 和会话队列恢复。

扫描失败不会阻止主 UI ready，而是产生不含异常原文的 unavailable snapshot。新会话在欢迎页
显示稳定降级状态，不用空白时间线替换启动体验；已有启动时间线时才追加独立告警卡。Bridge 不调用
`resume_sandbox_retry()`，也不根据 catalog 文本判断 ticket 是否可接管。

启动调试路径只保存为 `/debug` 可查询状态，不再抢占欢迎页成为历史消息；因此即使 debug trace
先于 ready 到达，用户仍能看到确定的“已就绪”身份与恢复降级状态。

### Textual TUI

TUI 在 `start_long_running_services()` 成功后调用同一个 Harness Service 方法。非空队列挂载为
Semantic Markdown，并在 StatusBar 显示“可显式恢复”计数。扫描失败显示降级提示；空队列不制造启动噪声。

TUI 与 New UI 都使用 HAR-08.4o3e 的共享 Slash：

```text
/harness eval sandbox resume <retry-action>
  --dispatch <dispatch-id>
  --receipt <retry-receipt-id>
  --sha256 <retry-receipt-sha256>
```

## New UI 用户体验

New UI 在 ready 时：

- 保存类型化 `sandboxRetryRecovery` 快照；
- 非空队列生成一张系统提示卡；
- 展示分类计数、Batch/Suite、H5a 和精确命令；
- live ticket 明确不提供并发恢复命令；
- reconcile/clock regression 明确要求查看完整 fence；
- 截断时提示使用只读 catalog 有界翻页；
- 不切换当前 route，不恢复上一进程的侧栏/详情页，不自动发送任何命令。

## 真实重启验收

聚焦集成场景使用真实 Git 工作区与真实 Harness SQLite：

1. 持久化不可变 Sandbox Eval Request Manifest；
2. 创建并取消 source admission；
3. 接受 retry intent，使 pending dispatch 与 retry receipt 原子落盘；
4. 丢弃首个 Store 对象；
5. 用新的 `HarnessStore`、`HarnessService` 和 `JsonlEngineBridge` 启动；
6. ready snapshot 发现同一个 pending dispatch，并生成精确 resume 命令；
7. 启动前后 dispatch 对象完全相同；
8. ticket ID 仍为空、dispatch epoch 仍为 0，证明启动扫描没有 claim。

## 验收证据

- Python 快照覆盖五种 open recovery 分类、20 项上限和截断语义；
- snapshot 不泄露 workspace、owner 或 execution authority；
- command/count/digest 篡改均失败关闭；
- unavailable 只暴露稳定错误码；
- Bridge ready 在扫描成功时携带完整快照且不调用 resume；
- Bridge 扫描异常仍发布 ready，异常原文不进入协议；
- TUI 使用同一 Service/renderer 语义显示人工恢复队列；
- New UI 重新计算摘要并拒绝不一致协议；
- New UI ready 只增加提示卡，不改变 route、不派发命令；
- 真实 Git + SQLite + 新 Store/Service/Bridge 重启场景证明只发现、不 claim；
- focused ruff、Python 小模块测试、JavaScript syntax/protocol/state 测试通过。

## 自我审视与未完成

已确认：

- 没有把 catalog cursor、snapshot SHA 或 UI 命令当成执行权限；
- 没有自动恢复 live、reconcile_required 或 clock_regression 项；
- 没有在 Bridge 与 TUI 各自读取 SQLite；
- 没有持久化瞬态恢复队列或恢复旧 UI route；
- 没有用 UI mock 替代真实 Store 重启证据。

仍未实现：

- New UI 专用 typed resume action/button；当前使用可复制的共享 Slash；
- dispatch detail 已由 HAR-08.4o3g 通过共享 Tool/Slash 交付；专用 typed 页面仍未实现；
- retry dispatch retention preview 已由 HAR-08.4o3h 交付；prune receipt/执行仍未实现；
- 跨主机 Sandbox admission；
- macOS/Linux/Windows 三平台真实隔离 Worker CI。

HAR-08.4o3g/4o3h 已交付只读详情、完整 protection refs 与有界 retention preview。下一切片
HAR-08.4o3i 应设计显式 prune receipt authority，不能因为队列或 preview 已可见就自动删除 dispatch。
