# HAR-08.5c1 Live Eval 类型化进度与双端展示

## 状态

已实现。本切片把 HAR-08.5b 的真实执行和 H5a 持久化阶段投影为严格、隐私有界的 runtime event，并让
New UI 与 Textual TUI 消费同一事实。它不新增第二条执行路径：付费调用仍必须先经过
`harness_eval_live_batch` Tool 的权限检查，normal 一次确认，bypass 直通。

## 为什么是当前最小切片

- Harness 8.5b 已有 Runner、Service、H5a 和终态回执，但用户在多次真实模型调用期间只能看到通用工具动画。
- UI-17 要求 New UI/TUI 对同一运行事实保持一致；现有 `harness/eval-batch` 页面已经承载 Static 与 Sandbox。
- ARC-01.3e 已提供封闭 Runtime Event 和 awaited sink；复用该边界不需要先扩张 ARC 或新增 UI 直连付费入口。

因此本切片只补进度协议与投影，不同时实现 Provider 账单 API、远端取消证明、历史目录或 Provider matrix。

## 权威进度模型

`HarnessLiveBatchProgress` 使用严格冻结 schema，阶段闭集为：

```text
preparing -> evaluating* -> persisting* -> completed | partial | error
```

每个 snapshot 绑定：

- Live batch request id 与 SHA-256；
- batch/suite/requested model 与 Provider 实际模型；
- requested/completed/persisted；
- 回执确认的调用数、token、带来源的记录成本、总成本/时限上限；
- actual-cost-exceeded、identity、Baseline eligibility、稳定 code/message。

协议机械校验 `persisted <= completed <= requested`、完整终态、超支事实和 Baseline 资格。进度不包含 Prompt、
模型输出、reasoning、工作区内容、API key 或 Provider 私有异常。

## 执行与权限链

1. Slash/New UI/TUI 仍提交共享命令并进入 `AgentEngine.execute_tool()`。
2. Engine 按现有权限策略确认 `harness_eval_live_batch`；Bridge 不提供绕过权限的 Live request handler。
3. Runner 在批次请求落定后发布 preparing，并在每个样本产生真实回执后发布 evaluating。
4. Service 每确认一个 H5a immutable sample 后发布 persisting，最终从防篡改 Batch Status 生成 terminal。
5. Tool 将 snapshot 序列化为 `harness_live_eval_progress` Runtime Event。
6. Bridge 同时保留通用 engine event，并投影到既有 `harness/eval-batch` Server Event；New UI 使用
   `kind=live` 打开专用内容，TUI 更新同源状态栏。

进度回调失败只记录低敏告警，不改变付费执行结果或造成隐式重试。调用方本地取消会传播
`CancelledError`，同时尽力发布 `partial/live_batch_cancelled_locally`；该状态明确不声称 Provider 已停止远端
推理或已完成最终计费。`total_calls` 表示收到 transport receipt 的确认调用，取消中的未知调用必须结合该
提示理解，不能被当作零远端成本证明。

## New UI

既有 Harness Eval Batch 页面增加 `kind=live`：

- 展示阶段、样本与 H5a 保存进度；
- 分色展示请求模型、Provider 实际模型、回执确认调用、token、成本/时限；
- 展示 batch request 和终态 identity 摘要；
- 明示不展示 Prompt、输出和思考内容；
- 只消费严格协议字段，丢弃额外/private payload；
- Live 事件到达时从 conversation 自动进入 Batch 页面，Esc 恢复原滚动锚点。

## Textual TUI

Textual 通过相同 Runtime Event 更新持久状态栏：阶段、completed/requested、persisted、记录成本和 batch id。
HAR-08.5c2 已将这里的成本明确区分为 catalog 估算或 Provider 账单来源；TUI 不解析 Tool 文案，也不自行
重新估算成本。

## 验收标准

- 成功 5 样本必须出现 preparing、逐样本 evaluating、逐样本 persisting 和 completed。
- completed snapshot 必须与终态 receipt 的 identity、成本、调用数和 Baseline 资格一致。
- Tool runtime event 不含挑战、原始响应或 reasoning；回调故障不重复调用 Provider。
- 本地取消传播 cancellation，并产生不冒充远端取消/计费证明的 partial snapshot。
- Bridge 同时输出 engine event 与 typed batch event；New UI 严格拒绝伪造超支/终态；TUI 显示同源成本。
- 只运行相关 Python/Node 小模块测试，不运行全量测试。

## 权威代码

- Progress/Runner：`src/naumi_agent/harness/eval_live_suite.py`
- Service/Tool：`src/naumi_agent/harness/service.py`、`src/naumi_agent/harness/tools.py`
- Runtime event：`src/naumi_agent/runtime/ports/events.py`、`src/naumi_agent/streaming/events.py`
- Bridge/TUI：`src/naumi_agent/ui/bridge.py`、`src/naumi_agent/tui/app.py`
- New UI：`frontend/terminal-ui/src/protocol.js`、`state.js`、
  `components/harness-eval-batch-page.js`

## 未完成

1. Provider cancellation/billing API 对账和远端 request id 尚未建立。
2. Live 历史查询仍复用 H5a/终态事实，没有专用分页目录。
3. macOS/Linux/Windows 的主流 Provider 小额真实矩阵仍未执行。
4. 当前 UI 只能取消整个前台 Agent run，尚无 Live batch 独立 durable cancel authority。

下一切片应先做 HAR-08.5c2 Provider request/billing evidence contract；若 Provider 适配层无法提供可靠标识，
则先交付显式 `unsupported` 能力矩阵，不能用本地 timeout 冒充远端取消证明。
