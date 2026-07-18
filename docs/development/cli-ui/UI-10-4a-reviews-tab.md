# UI-10.4a Workbench Reviews 只读证据页

## 交付目标

把已经存在于 Workbench Store 的 waiting approval 和 ReviewEvidenceCollector 接入 New UI 与
Textual TUI，让用户能在终端中看见“为什么需要审查、实际改了什么、验证是否通过、当前阻塞在哪里”。
本切片只建立可信观察面，不执行审批动作。

## 权威数据流

```text
WorkbenchStore waiting approvals
  -> WorkbenchService.dashboard_snapshot()
  -> workbench/snapshot
  -> Reviews list

selected approval id
  -> workbench/review/request
  -> WorkbenchService.get_review_evidence()
  -> ReviewEvidenceCollector (Store + managed worktree git diff)
  -> workbench/review
  -> selected Review detail
```

New UI 和 TUI 不读取 SQLite、不自行运行 Git、不从自然语言推断验证状态。列表与详情允许分开刷新，
因此 100 个待审项不会触发 100 次 Git diff。

## 协议契约

客户端请求：

```json
{
  "type": "workbench/review/request",
  "payload": {"session_id": "current-session", "review_id": "approval-id"}
}
```

服务端响应包含 schema version、session id、review id、`ready|unavailable` 状态和有界 evidence。
Bridge 必须拒绝跨会话读取；approval id 必须与请求 id 一致。Node 只保留下列公开字段：

- approval 基本字段；
- issue 的任务、风险和变更载体引用；
- worktree 名称、路径、`present|missing|unbound`；
- 最多 20 次 validation run；
- 最多 200 个 changed file；
- 最多 30 个 diff hunk，每个最多 4000 字符；
- 最多 50 条 agent note 与 event 的公开摘要。

任意未知私有字段在 Node 边界丢弃。异常信息不穿透到 UI，只使用稳定中文错误和诊断码。

## 用户体验

- 页签：`1 概览`、`2 Worktrees`、`3 Reviews`。
- 导航：方向键逐项，PageUp/PageDown 每次十项，Home/End 到边界，`r` 刷新，Esc 返回。
- 宽屏：左侧列表、右侧证据；窄屏：短列表后接当前证据。
- 文件：新增绿色、删除红色、修改黄色、重命名青色。
- Diff：新增行绿色、删除行红色、hunk 青色、元数据弱化。
- 阻塞：worktree 不可用为红色；无验证为黄色；验证失败为红色；证据齐备为绿色。
- 空状态明确说明当前无待审项；页面明确说明审批动作尚未开放。

## 安全与边界

- 只读，不调用 resolve approval，不创建 Proposal/Issue，不写 Git。
- worktree 路径必须解析在配置的 managed worktree 根目录内；`../` 逃逸返回 missing，不读取外部目录。
- Git diff 以 `HEAD` 为基线，同时覆盖 staged 与 unstaged 的已跟踪文件变更；untracked 文件进入文件列表，
  不伪造不存在的 patch。
- Review 页面展示真实证据，不把“测试通过”等同于“应当批准”。
- bypass 不改变本页只读属性；未来 UI-10.6 即使在 bypass 下执行动作，也必须留下决策审计。
- 页面选择、详情缓存和页签均不持久化到新 Session。

## 验收标准

- 0/1/100 个 Review 在 80/120/200 列可导航且每行不溢出。
- 选择变化只请求所选 review；旧 response 不得覆盖新选择。
- 当前 session、review id 和 schema 不匹配时拒绝数据。
- 真实临时 Git worktree 的修改文件和 unified diff 能进入 UI；路径逃逸不能读取根目录外内容。
- failed、no validation、missing/unbound worktree 和 ready 四种证据门状态可区分。
- New UI 与 Textual TUI 使用同一个 Workbench Service。
- 不出现 approve/reject 控件，不产生 Git/Store 写操作。

## 后续依赖

- UI-10.5 可复用 Review detail 中的事件摘要进入统一 Timeline。
- UI-10.6 在本页之上增加预览、确认、approve/reject/cancel；动作必须走 Python 权限与审计。
- HAR-09.4/09.5 后续应把 Evolution Proposal 适配进现有 Workbench 治理对象，而不是新建第二套 Review UI。

## 后续实现状态（2026-07-18）

UI-10.6a 已在本只读基础之上把 open Proposal 合并进同一 Reviews 列表，并实现
approve/reject/cancel。Approval 证据链仍保持只读，Proposal 动作走 Python 权限、既有 Service 与审计；
详见 `UI-10-6a-proposal-actions.md`。本文件描述的 UI-10.4a 验收边界不被追溯改写。
