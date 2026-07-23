# HAR-08.4o3c Sandbox Retry UI Action

## 状态

已实现，2026-07-23。

本切片把 HAR-08.4o3b 的真实 retry Tool 投影为共享 terminal protocol 和 New UI 可操作回执。用户在
Sandbox Eval 页面取消当前 ticket 后，不需要复制原 checks、samples、batch 或 revision；按 `R` 即可用
accepted cancel receipt 恢复原请求。Textual TUI 继续使用同一共享 Slash 命令并显示同一 typed progress。

## 协议

新增 client event：

```text
harness/eval-sandbox/retry
```

严格 payload：

- `action_id`: `hsar_<24 hex>`
- `cancel_receipt_id`: `hsacr_<24 hex>`
- `cancel_receipt_sha256`: 64 位 SHA-256
- `reason`: 1..500 字符

客户端不能发送 workspace、batch、suite、checks、samples、revision、execution authority、ticket 或 Run
Grant。

新增 server event：

```text
harness/eval-sandbox/retry-result
```

结果只投影 durable authority：

- retry receipt ID/SHA、decision/code；
- cancel receipt ID/SHA 与 source ticket；
- 原 `eval_request_sha256`；
- 新 `execution_authority_key`；
- dispatch ID/state/epoch/ticket fence；
- 原 batch 的 requested/persisted H5a 计数；
- `completed/failed/cancelled/rejected/blocked` outcome。

Python 与 JavaScript 双端都拒绝 receipt/dispatch/action/authority 不一致，以及 completed dispatch 缺少完整
H5a 的 payload。

protocol contract 已登记两个 experimental Harness 事件，并把前一 registry digest 写入兼容账本。

## Bridge

`JsonlEngineBridge.start_harness_eval_sandbox_retry()`：

1. 限制最多 4 个并行 retry control tasks；
2. 为当前 session 生成 `manual:<session>` run ID；
3. 构造 `harness_eval_sandbox_retry` ToolCall；
4. 调用 `AgentEngine.execute_tool()`，不直连 Service/Store 执行路径；
5. 把 Tool 的 typed Sandbox progress 关联到当前 request ID；
6. Tool 结束后重新读取 retry receipt、dispatch、Request Manifest 与 H5a；
7. 生成 typed retry result；
8. 无 receipt 时明确返回权限/authority error；
9. Bridge 关闭时有界取消并回收 retry tasks。

因此，New UI 不绕过 PermissionChecker，也不复制 HAR-08.4o2/4o3a/4o3b 的业务逻辑。

## New UI

Sandbox Eval 页面现在：

- cancel receipt 显示 ID 与 SHA 摘要；
- accepted cancel receipt 显示 `R 使用该回执恢复原请求`；
- retry pending 时显示新权威正在调度；
- typed result 显示 outcome、code、新 ticket/epoch 和 H5a 计数；
- retry 已 accepted 后不允许重复消费同一 cancel receipt；
- rejected 或 transport error 不伪装已消费，用户可以刷新后重新提交新 action；
- retry 运行期间的新 ticket 仍可用 `C` 取消；新 cancel receipt 可形成下一条显式 retry chain；
- Bridge recovery 在 cancel/retry control operation 未终态时失败关闭，不自动重放。

## Textual TUI

Textual TUI 没有复制一套 Store 调用：

- 用户通过 `/harness eval sandbox retry <receipt> --sha256 <digest> [--reason]` 触发；
- 共享 Slash 路由仍构造同一个 ToolCall；
- `_TuiSlashCommandFrontend.update_harness_sandbox_eval()` 展示 retry 的
  admitted/recovering/acquiring/executing/completed 状态；
- 最终 Markdown receipt 与 New UI 使用同一 Service 结果。

后续若新增 Textual 专用详情页，只能消费本切片 typed result，不能重写 retry authority。

## 验收证据

- Python client protocol 接受精确 retry payload，拒绝错误 action/receipt/SHA/空 reason；
- Python payload projector 绑定 receipt、dispatch 与 H5a，拒绝 authority 漂移；
- Bridge 测试证明执行入口是 `harness_eval_sandbox_retry` Tool，并关联进度与终态 request ID；
- JavaScript protocol 严格校验 accepted authority、dispatch fence 和完整 H5a；
- New UI state 测试证明 `R` 只发送 cancel receipt，不发送原业务参数；
- typed result 结束 single-flight pending；
- transport error 结束 pending 且保留 rejected 状态；
- 页面渲染 action、receipt SHA、新 ticket 和 5/5 H5a；
- Bridge recovery 把 retry pending 视为不可安全自动恢复；
- protocol registry Python/JavaScript 覆盖一致。

HAR-08.4o3b 的真实 Git + 隔离 Worker 场景已经证明 2/5 取消后续跑到 5/5；本切片没有用 UI mock 替代该
后端证据。

## 自我审视与限制

已确认：

- 前端没有客户端重述原请求；
- Bridge 没有绕过 Tool/Permission；
- retry result 来自重新读取 durable Store，而不是解析 Tool 文本；
- New UI 和 TUI 共享执行权威与进度协议；
- 同一 cancel receipt 不会因按键重复而并发消费。

仍未实现：

- HAR-08.4o3d 已补齐 retry dispatch catalog 与共享 Slash；专用历史详情和 retention 仍未完成；
- Bridge 重启后的 retry control-task 主动恢复；HAR-08.4o3e 已提供 receipt-bound resume Tool/共享
  Slash，HAR-08.4o3f 已提供启动有界 snapshot 与 New UI/TUI 人工恢复队列；UI 仍不自动重放
  不确定请求，专用 typed action/button 尚未实现；
- 跨主机 admission；
- Linux/Windows 的真实隔离 Worker CI 证据。

HAR-08.4o3d/4o3e/4o3f 已交付跨进程恢复所需的 catalog、显式恢复权威与启动发现快照。下一切片应
横向比较只读 dispatch detail 与 retention preview，不继续扩展页面装饰。
