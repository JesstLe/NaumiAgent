# HAR-08.8e2 Typed Eval Batch 真实进度

## 1. 目标

让重复 Eval Candidate Batch 在评测和持久化期间提供真实、可验证的逐样本进度，并同步新 UI、Textual
TUI 与兼容终端的最终状态。本模块不以定时动画猜测进度，也不改变 H5a immutable Store 规则。

## 2. 执行链路

`evaluate_suite_repetitions()` 在同一 source identity boundary 内，每完成一个真实 raw sample 调用同步
`on_sample(completed, result)`。`HarnessService` 用线程安全队列把工作线程事实转回 asyncio 事件循环，依次
产生：

1. `preparing`：Profile、Suite、Store 与参数预检完成；
2. `evaluating`：每个真实 sample 完成后更新 `completed` 和 Case 汇总；
3. 完整 source-after 复核和 Identity 附加；
4. `persisting`：每个 H5a immutable sample 写入成功后更新 `persisted`；
5. `completed/partial/error`：以端到端耗时、Identity 和晋升资格结束。

partial 或不完整持久化永远不可晋升。进度观察器失败只记录告警，不中断已经开始的权威 Eval/Store 操作。

## 3. Bridge 与并发

- 请求：`harness/eval-batch/request`；
- 事件：`harness/eval-batch`；
- Bridge 把每个 Batch 放入独立 asyncio task，因此 stdin、心跳、对话和其他控制事件保持响应；
- 单个 UI 进程最多并行 4 个 Batch，超过后返回 `harness_eval_batch_limit`，不会静默排队或过载；
- UI 关闭时取消并回收仍在运行的 Bridge Batch task；已写入的 immutable prefix 保留，并继续被晋升 gate
  判定为不完整。

每个事件严格校验 stage/terminal、5..100 requested、`persisted <= completed <= requested`、完整终态计数、
安全 Batch/Suite ID、有限耗时和可选 Identity SHA-256。未知私有字段不会进入前端状态树。

## 4. 用户体验

新 UI 输入 `/harness eval <suite> --repeat N [--batch id]` 打开独立进度页，展示百分比、评测/保存双计数、
Case 汇总、Identity、资格、端到端耗时和 terminal 下一步。80/120/200 列均保持可读，颜色之外保留文字。

Textual TUI 通过共享 Slash frontend 回调实时更新持久状态栏，例如
`Eval Batch 评测: 2/5 · 已保存 0`；完成后仍由共享 `render_eval_batch_status()` 输出最终 Markdown。
兼容终端无进度 frontend 时行为不变。

## 5. 验收

- runner 回调严格为 1..N，且只在真实 sample 完成后触发；
- 真实生产 hello Suite 的 TUI Slash 路径产生 preparing、5 次 evaluating、5 次 persisting；
- Bridge 在 Batch 阻塞时仍立即响应 ping，最多允许 4 个并行任务；
- terminal completed 必须满足 completed=persisted=requested；
- partial/incomplete 不显示可晋升；
- 新 UI 正确展示中间 40% 和最终 100%，source 复核前不伪造 Identity；
- Python/Node 定向测试、语法检查和跨语言真实烟测通过。

## 6. 后续

- HAR-08.8e3 已完成：晋升理由交互、typed 结果与 selector 变化；
- HAR-08.8e4：Comparison receipt 详情与筛选；
- HAR-10：把同一 task 生命周期、心跳和恢复模式推广到长时 Live/Sandbox Eval。
