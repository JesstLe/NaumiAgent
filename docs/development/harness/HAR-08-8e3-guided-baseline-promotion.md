# HAR-08.8e3 引导式 Baseline 晋升

## 1. 目标

在不建立第二套晋升逻辑的前提下，把 H5b 的显式治理动作接入新 UI 与 Textual TUI 的结构化交互。
用户输入 `/harness baseline promote <suite> <batch>` 后先选择或填写理由，再做最终确认；取消、协议错误或
eligibility 拒绝都必须明确说明 selector 未改变。已经提供 `--reason` 的显式命令与 Agent Tool 保持直达，
不增加重复确认，也不改变 bypass 的全权限语义。

## 2. 共享执行链路

`run_eval_promotion_flow()` 是 UI 编排层，只负责：

1. 规范化 Suite、Batch 和可选理由；
2. 通过现有 `UserInteractionRequest` 收集推荐理由或 3..2000 字符自定义理由；
3. 再次展示精确 Suite、Batch 和理由，要求确认或取消；
4. 仅在确认后调用一次 `HarnessService.promote_eval_baseline(actor="user")`；
5. 把 H5b 的 `promoted/already_active/not_selected/error` 转成 typed flow 终态。

Store、版本号、eligibility、selector 原子切换与审计事件仍只有 H5b 一套权威实现。Agent Tool 继续直接调用
Service，并以固定 `actor="agent"` 记录事实。

## 3. 协议与并发

- 请求：`harness/eval-promotion/request`，包含安全 Suite/Batch 和可选 reason；
- 事件：`harness/eval-promotion`，阶段为 `awaiting_reason`、`awaiting_confirmation` 或权威终态；
- 等待确认必须携带最终理由；成功终态必须携带 Baseline ID、版本、样本数、操作者、理由和时间；
- Bridge 在独立 asyncio task 中等待交互，心跳、输入和其他任务不被阻塞；
- 单进程最多并行 4 个晋升交互；重复 request id 和超限请求被明确拒绝；
- 关闭 UI 时取消并回收等待任务；确认前不会触碰 selector。

## 4. 用户体验

新 UI 打开独立晋升页，并复用全局交互卡片完成理由选择、自定义输入和最终确认。页面在 80/120/200 列
展示 Suite、Batch、当前阶段、最终理由、版本、Baseline、操作者与时间；取消或错误用文字明确显示
`Selector 未改变`，颜色只作辅助。

Textual TUI 通过 `_TuiSlashCommandFrontend.request_user_interaction()` 复用现有 modal；兼容终端没有结构化
交互宿主时提示改用 `--reason <原因>`。显式理由路径不会弹出交互。

## 5. 验收标准

- 推荐理由、自定义理由都必须经过最终确认后才调用 Service；
- 理由步骤取消、确认步骤取消、过短自定义理由均不调用 Service；
- Bridge 依次发送两种等待阶段和一个权威终态，并保持请求关联；
- 前端拒绝缺失理由的确认态和缺失审计字段的成功态；未知私有字段不进入状态树；
- 真实受信 hello Suite 完成五次 Eval 后，可经共享 TUI frontend 晋升并读取到 active selector；
- 新 UI 页面在常见宽度不溢出，取消态明确显示 selector 未改变；
- 仅运行本切片的 Python/Node、ruff 和语法检查，不以全量测试代替定向证据。

## 6. 后续

- HAR-08.8e4：Comparison receipt typed 详情、decision/时间筛选与跳转；
- HAR-09/EVO-03：改进候选只能引用 Baseline/Comparison 证据，不得绕过人工晋升治理；
- HAR-10/ARC-06：把等待交互的租约、恢复与背压推广到跨进程长周期任务。
