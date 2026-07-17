# HAR-08.8e1 Typed Baseline 状态页

## 1. 目标

把 HAR-08.8a 的权威 Baseline 读取能力接入新 UI 的 typed Bridge，并保持 Textual TUI/兼容终端
继续复用同一个 `HarnessService.eval_baseline_status()`。本切片只做只读状态页，不把 Batch 执行、晋升
或 Comparison 创建藏在页面刷新中。

## 2. 协议

- 请求：`harness/eval-baseline/request`，仅接受严格 Suite ID；
- 响应：`harness/eval-baseline`，包含 `schema_version=1`、确定性 `snapshot_sha256`、状态、active
  Baseline 和最多 20 条只引用当前 active 的 Comparison；
- `ok` 必须包含 active；`empty/unavailable` 禁止携带 active 或 Comparison；
- 前端再次验证 SHA-256、版本、样本数、decision 以及每条 Comparison 的 `baseline_id`；私有和未知字段
  不进入状态树。

`snapshot_sha256` 对公开字段 canonical JSON 计算，内容不变时稳定，任一 active/Comparison 事实变化时
改变；它不是时间戳，也不伪装成 Store 的全局单调 revision。

## 3. 用户体验

新 UI 输入 `/harness baseline <suite>` 直接打开可滚动页面，区分加载、空状态、状态库不可用和正常状态。
正常页用不同颜色表达通过、失败、波动、证据不足和不可比较，同时保留完整文字标签；窄至 80 列仍不
越界。`Esc` 返回进入页面前的对话滚动位置。

Textual TUI 与兼容终端仍通过同一 Slash 路由渲染 `HarnessEvalBaselineStatus`，因此业务状态、active
选择和 Comparison 过滤规则一致；颜色和布局由各前端负责，不复制 Store 查询或 decision 逻辑。

## 4. 验收

- Python serializer 对相同状态产生相同 snapshot digest，限制文本和 Comparison 数量；
- Bridge 对真实 Service 返回 typed snapshot，异常只暴露中文 unavailable 状态；
- 前端拒绝错误摘要、非法 Suite、零版本/零样本和跨 Baseline Comparison；
- 新 UI 能在 80/120/200 列展示 active 与 Comparison，并正确展示 loading/empty/unavailable；
- 恢复其他会话时清空页面和 workspace-scoped snapshot cache；
- 协议、Bridge、状态、页面和语法定向测试通过。

## 5. 后续边界

- HAR-08.8e2 已完成：Batch typed 真实进度与终态详情；
- HAR-08.8e3 已完成：晋升交互、理由输入与结果页；
- HAR-08.8e4：Comparison receipt 详情、decision/时间筛选；
- HAR-08.3/8.4/8.5：Replay、Sandbox、Live runner 复用这些 typed surface。
