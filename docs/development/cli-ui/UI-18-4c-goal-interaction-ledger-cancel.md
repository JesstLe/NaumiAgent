# UI-18.4c Goal Interaction Ledger 与显式取消

## 目标

把 HAR-10.6 durable interaction 从“只在问题出现时可见”推进为 Goal/Pursuit 页面中的可恢复状态：用户能看到
与当前可见 Pursuit 稳定关联的 pending/answered/expired/cancelled 历史，并显式取消仍在等待的问题。取消必须
经过 Harness authority 的 sequence fencing 与 append-only 哈希链，不能由前端乐观改状态。

## 权威链路

1. `HarnessInteractionRecord.cancelled` 由真实 `cancel_interaction()` 状态迁移产生，只允许 pending → cancelled。
2. `HarnessStore.cancel_interaction()` 复用统一 transition transaction、expected sequence、快照摘要和事件哈希链。
3. `list_interactions()` 以 workspace + subject kind/IDs 隔离、最新优先、最多 100 项读取，不隐式 expire 或 takeover。
4. Goal snapshot 只投影当前可见 Goal 所关联 Pursuit 的最近 50 条 interaction；不暴露 owner、答案正文或私有字段。
5. New UI 使用 `interaction_cancel` 协议；Bridge 成功提交 authority 后才关闭实时卡片并发出
   `interaction/resolved(status=cancelled)`。
6. CLI/Textual TUI 使用 `/goal interaction cancel <id>`，经 `goal_interaction_cancel` ToolExecution 访问同一
   Harness authority；Tool 还会核对 interaction 的 Pursuit 确实关联到当前 workspace 的 Goal。

## 用户体验

- Goal 页面按状态着色展示 interaction ID、标题、问题和时间状态。
- 只有 pending 项显示取消命令；终态项目不会伪装成可操作。
- 取消成功后实时卡片、Goal 快照本地状态与系统通知同步变为“已取消”。
- 未知 ID、非法 ID、非 pending、非 Goal/Pursuit 归属、并发 sequence 冲突和 authority 不可用均返回中文结果。

## 验收标准

- Store 测试证明取消恰好推进一个 sequence、移出 pending 列表、保留在历史中，并拒绝旧 sequence 重放。
- Goal producer 只输出稳定链接的 interaction，且无 owner/private 字段；Markdown fallback 给出同一取消命令。
- Python/Node 协议严格验证 ID、状态、sequence、归属和 `can_cancel` 一致性。
- Bridge 真实 Future 在 durable commit 后收到取消异常，卡片关闭，Store 终态可从新连接读取。
- Goal Tool 和共享 `/goal` dispatcher 支持相同取消命令，New UI 与 TUI fallback 不形成展示/动作断层。

## 保留边界

- 本切片不开放手动 takeover；有效 owner lease 仍由 Bridge/TUI 自动续租，死 owner 由现有 recovery 接管。
- 历史是最近 50 项的有界投影，尚无 cursor、筛选和独立详情页。
- 取消后的 Pursuit checkpoint 由既有 resume reconcile 判为 cancelled；本切片不自动 resume，也不隐式消耗模型轮次。
