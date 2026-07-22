# UI-17.2d Evaluation Lane Receipt Golden Scenario

## 1. 目标

把 EVO-03.7a 的单 lane 权威回执接入默认 New UI，并锁定 Textual TUI fallback 的同源公开语义。用户输入
`/evolution evaluation <comparison-id>` 时，New UI 不把命令提交为聊天消息，而是发出 typed request 并打开专页；
TUI 与兼容终端继续调用同一个 `EvolutionEvaluationLaneReceiptExecutor` 和 Markdown renderer。

本切片只展示已经持久化并重新校验的 RED/GREEN lane 事实，不签发候选最终 Evaluation Receipt，也不提前实现
EVO-03.7b 跨 lane 聚合。

## 2. Authority 与协议

- Python Bridge 接收 `evolution/evaluation-lane/request`，只接受 64 位小写 Comparison SHA-256，并按当前
  `workspace_root` 调用 `execute_by_id()`；跨工作区、缺失、损坏或不完整证据统一返回稳定错误码
  `evolution_evaluation_lane_failed`，不泄露内部异常和路径。
- Server event `evolution/evaluation-lane` 是 path-free 有界投影：包含 receipt/plan/candidate/comparison/attribution
  摘要、RED/GREEN cohort、六项有序 artifact、时间范围与资源覆盖；不下发 `workspace_root`，也不复制嵌套的完整
  H5c 和 Failure Attribution 源回执。
- Node normalizer 严格校验枚举、ID、digest、cohort 状态计数、token/cost coverage、artifact 顺序及交叉引用；
  `candidate_evaluation_complete` 必须为 `false`，`aggregation_required` 必须为 `true`。
- 两个事件已进入发布 protocol contract 与 event registry，owner 为 `evolution`，当前 stability 为
  `experimental`。

## 3. 用户体验

New UI 专页使用红色展示 RED/失败证据，绿色展示 GREEN/通过与改善，黄色展示证据不足或“非最终结论”，紫色展示
不可比较状态。页面在 80、120、200 列宽下换行且不越界，并提供 `R` 刷新、方向键/PgUp/PgDn 滚动和 `Esc` 返回。
加载、空响应和失败都有独立中文状态，不会落回聊天时间线伪装成功。

TUI fallback 仍渲染共享 Markdown 回执，明确写出“不是候选最终 Evaluation Receipt”和后续跨 lane 聚合要求。
这避免在 Textual 内复制第二套查询或判断逻辑。

## 4. Golden fixture 与验收

`tests/fixtures/ui17/evaluation-lane-receipt-golden.json` 是公开语义唯一 fixture，锁定：

- comparison `passed`、统计 `improved`；
- RED 5 个失败样本、GREEN 5 个通过样本；
- 实际观测 token 1000 → 500；
- finality 固定为 `false/true`；
- New UI 与 TUI 必须出现的关键可见标记。

验收链路：

1. Python protocol 拒绝越界或非 SHA-256 请求；Bridge 返回 path-free typed payload。
2. 真实 H5 SQLite 证据链生成完整 receipt，Slash/Agent Tool/TUI renderer 消费 fixture 的 TUI markers。
3. Node protocol 消费同一 fixture 并拒绝伪造 finality、artifact 摘要错配。
4. Node 页面在三种常见宽度渲染共享 markers，state 将命令拦截为 typed request，并验证刷新、返回和新会话清理。
5. 仅运行上述聚焦 Python/Node 测试、Ruff、compile 与 protocol registry 检查；不运行全量测试。

## 5. 边界与后续

本切片不证明 UI-17.2 全部完成，也不证明 Candidate 已通过完整 Evaluation。后续 EVO-03.7b1/3.7b2 已完成
Interventional 与必需平台 Adversarial lane 的后端聚合 authority，但专用 Final Evaluation typed 页面/golden
仍未实现；流式断流/error/retry golden 后续已由 UI-17.2e 完成，但 UI-17 仍缺断连/权限恢复、兼容协商、发布矩阵与
release gate。不得用页面颜色或
单 lane `passed` 替代聚合 authority。
