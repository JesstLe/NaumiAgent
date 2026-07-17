# HAR-08.8a Baseline 与 Comparison 只读状态面

## 1. 目标

让用户和 Agent 能从同一 `HarnessService` 查看指定 Suite 的 active Baseline 与最近 Comparison
receipts，而不直接读取 SQLite、不在 UI 重算 verdict，也不泄露绝对工作区路径或完整 Identity。

本切片只读；不运行 Eval、不创建 batch、不晋升 Baseline、不改变 selector。

## 2. 共享入口

- 用户：`/harness baseline <suite-id>`；
- Agent：`harness_eval_baseline(suite=...)`，read-only、concurrency-safe；
- Service：`HarnessService.eval_baseline_status()`；
- Renderer：`render_eval_baseline_status()`。

新 UI、Textual TUI 与兼容终端都复用现有 Slash router，所以用户入口不会形成三套查询逻辑；Agent Tool
也调用同一 Service 和 renderer。

## 3. 显示语义

- 无状态库：明确显示“不可用”，引导 `/harness doctor`；
- 无 Baseline：明确显示“尚无 Baseline”，不伪造 v0 或默认成功；
- 有 Baseline：显示版本、batch、sample count、短 Baseline/Identity digest、actor、时间和晋升原因；
- 最近比较：只查询 active Baseline，最多 20 条，显示 Candidate batch、中文 decision、统计 verdict、
  sample count 与短 receipt ID；旧版本 receipt 不混入当前版本；
- 不显示完整 workspace path、完整 receipt JSON、原始 Result、reasoning 或 secret。

Decision 文案区分通过、未通过、波动、无法判断与不可比较；即使终端关闭颜色，含义仍由文字表达。

## 4. 验收

- Slash 与 Agent Tool 对无 Baseline 返回同一可执行下一步；
- 真实 schema v10 Store 中的 v1 Baseline 和 passed receipt 经 Service 重启后可见；
- active version、Candidate batch、中文 verdict 与短 digest 正确渲染；
- Tool 缺少 suite、空值或超长 suite 在访问 Store 前拒绝；
- Store 损坏映射为 unavailable，不向 UI 抛出 SQLite 技术细节；
- 原有 Harness surface、H5c receipt 和相关 Store 小模块测试保持通过。

## 5. 后续

- HAR-08.8b 已完成：重复 Eval batch 运行与 H5a 自动持久化；
- HAR-08.8c 已完成：显式 Baseline promote，固定 actor 并要求 reason；
- HAR-08.8d 已完成：以 active Baseline 比较 Candidate 并生成 H5c receipt；
- HAR-08.8e：typed New UI/TUI 全屏 detail、筛选和 receipt drill-down；
- API 只复用上述 Service，不新增独立计算路径。
