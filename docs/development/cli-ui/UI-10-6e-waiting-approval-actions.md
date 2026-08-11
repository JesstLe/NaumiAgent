# UI-10.6e waiting Approval 人工决策闭环

## 目标

让 New UI 与 Textual TUI 可以在 Workbench Reviews 中对当前会话的 `waiting`
Approval 执行批准或拒绝，并保证并发、重试、bypass 和异常路径都服从同一个 Python
authority。该能力只收口人工审批事实，不执行代码、不创建实验、不授予发布或 promotion 权限。

## 权威边界

- New UI 与 Textual TUI 只收集人工意图；终态由 `WorkbenchStore` 决定。
- `WorkbenchService.resolve_approval()` 是两端共用的业务入口。
- Store 以 `BEGIN IMMEDIATE` 和 `state = waiting` 条件更新实现 compare-and-set。
- Approval 终态和 `approval.resolved` 审计事件在同一个 SQLite 事务中提交；不存在“状态已改、
  审计缺失”的可见窗口。
- 第二个并发决定、重复发送或迟到重试得到稳定的
  `approval_resolution_conflict`，不会覆盖先到终态，也不会追加第二条终态审计。
- 该动作属于 Human Control Plane。Agent 可以创建、观察和解释 Approval，但不得拥有自批
  Tool；否则“需要人工批准”会退化为 Agent 自己绕过 gate。用户手动入口由 New UI 与 TUI
  的 Reviews 键盘动作提供。

## 状态机

```text
waiting --approve--> approved
waiting --reject(reason)--> rejected
approved/rejected --any action--> conflict + authoritative snapshot
```

拒绝原因去除首尾空白后必须非空，最长 2000 字符；ID、会话、动作、确认位均使用严格协议字段，
未知字段和控制字符在 Bridge 之前拒绝。前端收到 completed 时还会验证 Approval ID、session、
终态和 Snapshot 的绑定，拒绝伪造或不完整回执。

## 权限与确认

- `moderate/permissive/strict`：PermissionChecker 要求一次明确确认。
- `bypass`：权限允许后直接提交，不显示高风险二次确认，但仍执行 session 校验、waiting CAS、
  拒绝原因校验和原子审计。
- `lockdown`：动作被阻止，Store 不发生写入。
- 跨会话 ID 在 Bridge 层以 `workbench_session_mismatch` 拒绝。

## New UI

- Reviews 选中 waiting Approval 后，`a` 批准、`x` 拒绝。
- 普通模式展示一次确认；拒绝先收集必填原因。
- bypass 批准立即提交；bypass 拒绝只收集必填原因，参数齐全后立即提交，不再增加确认页。
- loading 阶段屏蔽重复键；completed、conflict、blocked、not-found 和 error 使用不同结果文案。
- completed/conflict 必须应用 Bridge 返回的权威 Snapshot，不做乐观终态推断。
- 80/120/200 列均展示动作提示、输入、确认、加载和语义色。

## Textual TUI

- 与 New UI 使用相同的 `a/x` 入口、PermissionChecker 和 WorkbenchService。
- `ApprovalDecisionScreen` 不持久化草稿；新进程不会恢复旧原因或确认状态。
- bypass 批准不打开 modal；拒绝 modal 同时承担必填原因采集和最终提交，不存在第二层确认。
- CAS 冲突会重新读取当前会话 Snapshot，并保留可读错误，不覆盖已经落库的终态。

## 验收标准

1. 两个并发 approve/reject 只有一个成功，另一个得到 conflict。
2. 每个成功终态精确对应一条 `approval.resolved` 审计事件。
3. reject 缺少原因、非法动作、控制字符、未知字段和非布尔 confirmed 均在写入前失败。
4. 普通模式未确认时 Store 调用次数为零；确认后才写入。
5. bypass 批准以 `confirmed=false` 直接完成，但不能绕过 session、状态机或审计。
6. completed 回执包含匹配的 Approval 与最新 Snapshot；conflict 回执至少包含最新 Snapshot。
7. New UI 与 Textual TUI 均可从键盘完成全链路，并显示中文状态。
8. 真实 Bridge → WorkbenchService → SQLite 场景可读取 approved/rejected 终态及唯一审计事件。

## 定向验证

- `tests/unit/test_workbench_store.py`：round-trip、重复终态、真实 SQLite 并发。
- `tests/unit/test_permissions.py`：普通、bypass、lockdown 权限边界。
- `tests/unit/test_ui_protocol.py`：严格 client payload。
- `tests/unit/test_ui_bridge.py`：确认、bypass、跨会话、冲突与真实 SQLite 闭环。
- `frontend/terminal-ui/test/protocol.test.js`：严格 typed result 与权威绑定。
- `frontend/terminal-ui/test/state.test.js`：输入、确认、bypass 与 Snapshot reducer。
- `frontend/terminal-ui/test/render.test.js`：常见宽度的动作与语义色。
- `tests/unit/test_tui_workbench_overview.py`：Textual 原因校验与 bypass 无 modal。

本切片不运行全量测试；只运行上述相关小模块及 Ruff、语法、协议 registry 和文档检查。

## 未包含

- UI-10.5b Timeline revisioned 增量、cursor 和 gap recovery。
- Approval 批准后的自动代码执行、实验签发、发布或 promotion。
- Agent 自主批准人工 Approval。
- 非 SQLite 多节点共识；当前并发权威是单 Workbench SQLite Store。
