# UI-10.5b Workbench Timeline 增量恢复

## 目标与拆分

UI-10.5b 把 Workbench Timeline 从“打开页面时读取最近 50 条”推进为可断线恢复的增量事件流。该能力不能
复用 Dashboard 的 `stream_id/revision`：Dashboard revision 代表整页 Missions、Agents、Issues、Reviews、
Release 与 Timeline 的联合快照；仅收到一条 Timeline 事件时推进该 revision，会错误掩盖其他区域的缺失更新。

因此本模块采用独立的 Timeline `stream_id/cursor`，并按依赖拆成两个可独立验收的切片：

1. **UI-10.5b1（已实现）**：SQLite authority、旧库迁移、并发游标分配、Service replay window 与 gap 判定；
2. **UI-10.5b2（待实现）**：JSONL Bridge push/reconnect、New UI reducer、Textual TUI 同源刷新与端到端恢复。

## UI-10.5b1 权威模型

`workbench_audit_streams` 为每个真实产生过事件的 session 保存一个持久 `stream_id` 和 `latest_cursor`；
`workbench_audit_events.cursor` 是该 session 内从 1 开始的单调整数。

- 只读空 Timeline 不创建 stream，避免首次打开 `/workbench` 产生状态；
- 普通 `append_event()` 在 `BEGIN IMMEDIATE` 事务内分配 cursor 并写入脱敏事件；
- Approval 终态与 `approval.resolved` 仍在同一事务内完成，事件 cursor 不破坏原有 CAS 原子性；
- 多连接并发写同一 session 时由 SQLite 写锁串行分配，cursor 唯一且连续；
- 不同 session 独立从 1 开始，不能用另一 session 的 cursor 推断当前事件；
- 旧库按 `timestamp, rowid` 稳定回填 cursor，只在首次迁移创建 stream，重启不旋转 identity。

## Replay 契约

`WorkbenchService.timeline_replay_window()` 返回：

- `schema_version/session_id/stream_id`；
- `requested_cursor/earliest_cursor/latest_cursor`；
- `gap/gap_reason`；
- cursor 升序且最多 100 条的脱敏、任务信息增强事件。

恢复请求要求 `after_cursor >= 0`；非零 cursor 必须同时携带已知 stream identity。以下情况失败关闭并返回空
事件，不猜测补丁：

- `stream_changed`：客户端 stream 与权威 stream 不同；
- `stream_identity_required`：携带非零 cursor 却没有 stream；
- `cursor_ahead`：客户端 cursor 超过服务端权威值；
- `cursor_before_retention`：cursor 早于仍保留的最早事件；
- `stream_unavailable`：客户端声称有历史，但该 session 当前没有权威 stream。

limit 只允许 `1..100`，cursor 拒绝 bool、浮点和负数，stream identity 拒绝控制字符及超过 128 字符的输入。
Store 写入前的递归 secret redaction 继续生效，replay 不建立第二份未脱敏日志。

## UI-10.5b1 验收证据

- [x] 同 session 连续写入得到 `1,2,...`，不同 session 独立计数；
- [x] 12 个独立 Store 连接并发写入得到唯一连续 cursor；
- [x] 进程重建后 stream identity 与 cursor 可继续恢复；
- [x] legacy 无 cursor 表完成确定性迁移，二次启动不旋转 stream；
- [x] 空读取不创建 stream；非法 cursor、stream 与 limit 被拒绝；
- [x] stream changed、identity missing、cursor ahead、retention gap 均返回明确机械原因；
- [x] Approval 状态与审计事件 cursor 保持同事务；
- [x] Service 输出 JSON-ready severity、cursor 和同源 task projection。

## UI-10.5b2 待实现与验收

- Bridge 打开 Workbench 时发送完整快照并订阅当前 session 的 Timeline stream；关闭页面、切换 session、
  shutdown 时停止订阅，不能遗留轮询任务；
- 连续 cursor 发送 `workbench/event`，事件 envelope 同时绑定 session、stream 与 cursor；
- reconnect 从客户端最后确认 cursor 有界 replay；gap 时只发送一次恢复通知并强制完整 Snapshot；
- Timeline cursor 不推进 Dashboard revision；其他 Workbench 卡片变化仍必须由完整快照或各自 typed patch 更新；
- New UI 对重复事件幂等，对跳号/换流/跨会话事件失败关闭；Textual TUI 使用同一 Service 恢复契约；
- 真实 SQLite append → Bridge → New UI/TUI 的连续、断线、gap、重启场景完成小模块端到端验证。

## 当前诚实边界

UI-10.5b1 只建立了增量流的持久 authority，尚未声称 UI 已实时推送。当前 New UI/TUI 仍通过完整
Workbench Snapshot 获得 Timeline；Bridge producer、客户端 cursor 保存与 gap 后整页恢复属于 UI-10.5b2。
