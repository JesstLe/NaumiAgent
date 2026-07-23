# HAR-08.4k Sandbox Eval Typed Progress

## 目标

把 HAR-08.4f coordinator 已持久化确认的 checkpoint 变成唯一 Runtime 进度事实，并同步投影到 New UI 与
Textual TUI。前端不得根据动画时长、Tool output 或本地计数估算样本进度。

本切片覆盖两种生产入口：

- Agent 自主调用 `harness_eval_sandbox` Tool；
- 用户通过 CLI、TUI 或 New UI 调用共享 `/harness eval sandbox` Slash。

两条入口仍通过同一 Tool、Permission、Service、Coordinator、H5a 链路；类型化进度不是第二条执行路径。

## Runtime 权威

新增闭集事件：

```text
harness_sandbox_eval_progress
```

`HarnessEvalSandboxTool.execute()` 接收 Tool execution context 自动注入的 `event_callback`，将每个
`HarnessSandboxBatchCheckpoint` 机械转换为有界公开 payload。`RuntimeEventPublisher` 负责事件 identity、
session/run context、sequence 和 awaited transport；Tool 不直接调用任一具体前端。

公开字段只包括：

```text
kind = sandbox
stage = recovering | acquiring | executing | completed | failed
terminal
batch_id
check_ids
requested / persisted
checkpoint_id / checkpoint_sha256
authority_key
lane
run_id / run_grant_sha256
sample_result_sha256
code
updated_at
```

不公开 workspace path、Profile command、环境变量、原始输出、权限回执内容或 secret。`persisted` 与
`sample_result_sha256` 都来自 Store-confirmed 连续 H5a 前缀；前端不能自行递增。

## 共享 Slash 注入

`/harness eval sandbox` 仍构造 `ToolCall` 并调用 `engine.execute_tool()`。共享命令层只增加一个
`on_event` adapter：

- Textual TUI adapter 更新持久状态栏；
- New UI Bridge adapter 把同一 payload 绑定到原始 `request_id`；
- 无进度前端的 CLI 保持最终文本回执，不输出重复瞬时日志。

Coordinator 对进度消费者保持一秒有界等待并隔离投影失败，因此 UI 卡顿不会改变 Batch、H5a 或权限清理结果。

## New UI

New UI 沿用现有 Harness Eval 页面容器，但协议增加显式 `kind=sandbox` 分支：

- 命令仍通过 `submit` 进入共享 Slash backend，而不是调用只供普通 Eval 使用的
  `harness/eval-batch/request`；
- 页面只在收到第一个权威 checkpoint 后打开；Profile/参数/权限等 preflight 在 checkpoint 前失败时保留在
  对话页展示最终错误，不留下永远等待的空进度页；
- Bridge 同时保留 generic `engine/event`，并投影一个严格校验的 `harness/eval-batch` Sandbox payload；
- 页面显示真实 checkpoint stage、持久化样本数、checks、lane、checkpoint、authority、run/grant 和结果摘要数；
- 页面不显示普通 Eval 的 Case 汇总、Identity、Baseline eligibility 或晋升提示。

协议会拒绝 stage/terminal、failed/code、run/grant、persisted/digest 数量、completed 样本数不一致以及非法
identity、时间和范围。

## Textual TUI

Textual TUI 复用共享 Slash frontend adapter，在状态栏显示：

```text
Sandbox Eval <阶段>: <persisted>/<requested> · <batch-id>
```

阶段文案使用中文：恢复、申请 Worker、隔离执行、完成、失败。最终详细结果仍由共享
`render_sandbox_eval_batch_receipt()` 输出。

## 验收证据

小模块验证覆盖：

1. Runtime event vocabulary 与 transport 映射保持穷尽；
2. Python public payload 保留 checkpoint 事实并拒绝重复/非法 checks；
3. New UI 协议拒绝不一致样本摘要和终态；
4. New UI 有效 Slash 仍发送 `submit`，首个权威 checkpoint 到达后以相同 request id 打开类型化页面；
5. New UI renderer 不伪造 Case/Baseline 指标；
6. Bridge 将 Agent Tool Runtime event 同步投影为 typed Harness event；
7. 真实 macOS Shell Worker 执行五个 samples，观察到
   `recovering → acquiring → executing(1..5) → completed`，且每次 persisted 与 H5a 一致；
8. 原有 Tool、事件、普通 Eval 协议测试保持通过。

按用户要求未运行全量测试。

## 自我审视与剩余边界

- 已实现的是进程内 coordinator checkpoint 的双端可见性，不是 HAR-08.4 全部完成。
- `queued` 尚不是 coordinator checkpoint stage。当前 admission 在进入 coordinator 前等待，因此本切片没有
  伪造 queued 状态；应由后续跨进程 admission authority 发布真实排队位置。
- 当前事件为瞬时投影，恢复后权威结果仍以 H5a/Batch receipt 为准；尚未实现进度事件独立 replay。
- Linux、Windows 的真实隔离 Worker/终端视觉 CI 仍缺失。
- 取消和重试动作尚未进入 Sandbox typed 页面；必须在 durable admission/lease 语义完成后接入，不能只取消前端。

下一依赖切片应优先实现跨进程 Sandbox admission/queue authority，再接 typed queued/cancel/retry；不应继续扩张
纯视觉状态。
