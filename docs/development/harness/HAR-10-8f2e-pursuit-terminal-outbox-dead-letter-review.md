# HAR-10.8f2e Pursuit 终态 Outbox 死信审查目录

## 目标与依赖裁决

HAR-10.8f2d 已能可靠停止 poison record 的自动重试，但只向用户显示数量和最近失败码。直接增加 requeue
按钮会迫使 UI 传递内部 outbox/attempt 身份，也无法让用户在操作前确认具体失败事实。因此 8f2e 先交付
只读、认证、bounded、identity-redacted 的审查目录，作为后续权限化处置的最小前置。

本切片不改变 dead-letter authority，不新增重试、放弃、删除或 retention 行为。

## Store 全目录认证

`PursuitStore.terminal_outbox_dead_letter_catalog()` 从 failure heads 与 failure events 的 outbox ID 并集开始，
而不是只查询 `dead_letter=1`：

1. 并集扫描最多 10000 个 authority，超过上限失败关闭；
2. 每个 authority 都复验完整 failure hash chain、独立 head、claimed dispatch 引用和 pending outbox；
3. head 被伪造成非死信、事件缺失 head、head 缺失事件或链尾被删除时，整个目录不可用；
4. 只有当前 outbox 仍为 pending 且最新 failure 拥有 dead-letter authority 才进入 active catalog；
5. 认证全部 authority 后才按 `occurred_at/event_id` 稳定倒序，最多投影 20 条并返回真实 total/truncated。

因此伪造 head 不能把死信重新变成普通可领取项，也不能从用户审查目录中静默隐藏。

## 公开协议边界

Goal terminal-outbox projection 升级为 schema v3。每条 `dead_letters[]` 只包含：

- `dead_letter_id`：由已认证 failure facts 派生的稳定 `ptfail_...` 操作目标；
- `disposition`：`retry_exhausted` 或 `permanent`；
- bounded `failure_code`；
- failure event 次数与包含安全等待在内的总认领次数；
- 带时区的发生时间；
- 固定为 true 的 `manual_review_required`。

严禁投影 outbox ID、attempt/run ID、owner、claim epoch、任何 SHA-256、原始异常或模型文本。Python 与
JavaScript 都验证 ID、枚举、次数关系、时间、唯一性、目录总数和截断一致性。前端兼容 schema v1/v2：
旧协议没有目录时规范化为空；如果 v2 已报告死信数量，则标记为“目录截断/尚未提供”，不会伪造详情。

## New UI 与 TUI

New UI 和 Goal Tool/Textual fallback 使用同一 Goal projection，逐条红色显示：

```text
死信 ptfail_... · 机械不变量破坏 · lease_missing · 失败 1 次 / 总认领 3 次 · 2026-...
```

预算耗尽显示“重试预算耗尽”，永久不变量显示“机械不变量破坏”。超过 20 条时显示明确截断提示。
本切片交付时目录没有操作键；后续 HAR-10.8f2f 已以同一稳定 ID 增加权限化 exact requeue，目录本身仍保持
只读权威投影。

## 聚焦验收

- Store close/reopen 后 catalog 返回与 backlog 相同的 active dead-letter total；
- payload、head 或链尾篡改导致 catalog 失败关闭，不能静默隐藏或重新领取；
- catalog policy 拒绝无效 limit/scan limit；
- Python projection 不含内部身份，目录计数、唯一性、次数与时间强校验；
- New UI protocol 接受 v1/v2 并统一为 v3，拒绝 `manual_review_required=false` 等伪造事实；
- New UI/TUI 同源显示 dead-letter ID、分类、原因、次数和时间；
- 只运行 dead-letter Store、Goal projection、前端 protocol/render 小模块，不运行全量测试。

## 自我审视与后续边界

- `dead_letter_id` 现在是稳定的公开审查目标；后续 HAR-10.8f2f 已提供 exact requeue action receipt，任何
  界面仍不得绕过 ToolExecution 自行解除死信。
- catalog 为保证完整认证使用 10000 authority 硬上限；规模增长后需要认证分页 root，不能取消完整性检查。
- 当前仅列 active pending dead letters；已经由未来人工操作收口的历史事实仍保留在 failure chain，但尚无历史页。
- HAR-10.8f2f 已完成第一种 exact manual disposition：以 `dead_letter_id` 定位、复验最新 head、经
  ToolExecution 权限链生成不可变 requeue 回执；HAR-10.8f2g 又补齐 exact abandon。disposed history 与
  retention apply 仍未实现。
