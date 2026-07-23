# ARC-02.5b 终端事件 ACK 与游标恢复

## 用户结果

New UI 收到 `completion/receipt` 或 `harness/receipt` 后，会先把对应 session 的
`stream_id + cursor` 原子保存到 `.naumi/terminal-ui-state.json`，再向 Python Bridge 发送
`terminal_events/ack`。Bridge 将每个稳定客户端的最高 ACK 持久化到
`.naumi/terminal-events.db`。

空闲 Bridge 断开并重启后，New UI 的既有 `resume` 请求会携带：

- `terminal_event_client_id`：项目 UI 状态文件内稳定、不可猜测的客户端身份；
- `terminal_event_stream_id`：上次确认的权威事件流；
- `resume_after_cursor`：客户端已经持久消费的最高游标。

Bridge 只有在客户端请求游标、stream 与该 client 的服务端持久 ACK 完全一致，且仍在
4096 条保留窗口内时，才补发缺失的安全回执。ACK 缺失或不一致、游标早于窗口、超出最新位置，
或 stream 不一致时，Bridge 明确选择 `gap_snapshot`，以无持久游标的业务 Store 快照重建回执，
随后发布新的权威游标基线；不会信任客户端单方面声称的消费位置，也不会把窗口缺口解释成
“没有新事件”。

## 权威边界

### 服务端

`TerminalEventJournalStore` 继续是唯一事件流权威，并新增：

- `replay_window()`：在一个 SQLite 读视图中取得 stream、最早/最新游标、服务端 ACK、
  gap 原因和有序缺失记录；
- `terminal_event_acks`：按 `workspace + session + client` 保存单调 ACK；
- `acknowledge()`：要求 client/stream 格式正确、cursor 不超过权威最新值且不能回退；
- Store 重建后 ACK 和 replay 判定仍保持一致。

保留清理仍由固定安全上限控制。本切片不允许单个慢客户端无限阻止清理，也不把 ACK 表冒充多客户端
交付策略；slow-client retention policy 属于 ARC-02.5c/2.6。

### 客户端

New UI 状态 schema 升级到 v6，项目级保存一个 `tecli_` 客户端身份，每个 session 只保存
一个严格的 `tes_` stream 和正安全整数 cursor。旧 v1-v5 状态会生成新客户端身份，不伪造历史 ACK；
未知未来 schema 继续只读拒绝覆盖。

收到持久事件时：

1. 同 stream 下一游标才进入 reducer；
2. 已消费的重复事件不重复渲染，并重新 ACK 当前最高游标；
3. 当前恢复完成后出现 cursor 跳跃或 stream 变化会立即 fail closed；
4. 本地游标保存失败时不发送 ACK，并终止 New UI，交给 TUI fallback；
5. ACK 成功与否不改变回执的业务结论。

Textual TUI 直接消费 Python Engine/Store，不经过 JSONL transport，因此不持有虚假的客户端 cursor。

## 协议

- `terminal_event_cursor` 只声明稳定回执带权威游标；
- 新 capability `terminal_event_recovery` 单独协商 ACK 与恢复握手，避免新版
  New UI 向只支持游标信封的旧 Bridge 发送未知控制事件；
- 新 client event：`terminal_events/ack`；
- 新 server event：`terminal_events/recovery`；
- `resume` 以全有或全无方式接受 client/stream/cursor 三字段；
- `session/replayed.terminal_event_recovery` 在回执前声明
  `legacy_snapshot | cursor_replay | gap_snapshot`；
- `terminal_events/recovery` 在回执后声明
  `replay_complete | snapshot_complete`、窗口边界、`gap_reason` 和实际补发数量。

协议治理注册表将本次 additive digest 的上一版本加入兼容清单。旧 Bridge 未提供恢复字段时，
New UI 使用 `legacy_snapshot`，不会凭空发送 cursor ACK。

## 恢复顺序

### 窗口内

1. Store 在切换会话前核验 client、stream、cursor 与服务端 ACK；
2. Bridge 加载精确 session；
3. `session/replayed` 清空并恢复会话文本；
4. Bridge 直接使用 Store 中原始 `event_id/stream_id/cursor` 补发缺失回执；
5. `terminal_events/recovery(replay_complete)` 证明已到最新游标；
6. New UI 原子保存并 ACK 最新游标；
7. Bridge 才用 `runtime/status` 完成空闲重连。

### 窗口外

1. `session/replayed` 先声明 `gap_snapshot`；
2. New UI 丢弃旧 cursor，但保留明确恢复状态；
3. Bridge 从 Harness/ChatRun 业务 Store 发无 cursor 的权威回执快照，避免重新分配旧事件身份并造成
   非单调传输；
4. 只有两个业务权威均成功读取后，`snapshot_complete` 才给出当前 stream/latest cursor；
5. 任一权威缺失或读取失败时，Bridge 不发布新基线，New UI fail closed 到 fallback；
6. New UI 保存完整快照的新基线并 ACK，下一次恢复从该位置继续。

## 验收证据

- Store：窗口内补发、retention gap、stream 变化、ACK 缺失降级、ACK 重启持久性、相等 ACK
  幂等、回退/越界拒绝；
- Python 协议：resume 三字段全有或全无、正安全整数和 ACK 白名单字段；
- Bridge：只补缺失 cursor；窗口外快照不携带伪造 durable envelope；业务权威不完整时不建立
  新基线；ACK 持久化后返回关联响应；
- Node：恢复 payload 严格 schema、session cursor 状态迁移、v1-v5 状态迁移和 v6 client identity；
- 真实双进程：第一代 Bridge 发布 cursor 1 并在 ACK 后退出；第二代 Bridge 收到精确
  `resume_after_cursor=1`，只补 cursor 2，New UI 最终状态文件保存 cursor 2；
- 仅运行相关 Store、Bridge、协议、状态和三个进程恢复测试，未运行全量测试。

## 自我审视与未完成

- 已实现的是两类无敏感字段安全回执的精确事件恢复，不包含模型 token、tool result、permission、
  interaction 或运行中 progress；这些事件必须先建立脱敏、幂等和副作用合同。
- 活动运行断线仍 fail closed 到 TUI，不自动恢复模型流或外部工具；HAR-07.4b 因此保持 partial。
- ACK 已是增量恢复的服务端门槛，但尚未参与多客户端 retention、observer/controller lease
  或公平策略。
- stdio Bridge 仍是单客户端进程；Unix socket、Windows named pipe 和完整 Runtime Service 生命周期
  仍属于 ARC-02.1-2.4。
