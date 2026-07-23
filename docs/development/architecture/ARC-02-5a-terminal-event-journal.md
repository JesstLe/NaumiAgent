# ARC-02.5a 终端回执事件日志与稳定游标

## 用户结果

New UI 从 Python Bridge 收到的 `completion/receipt` 与 `harness/receipt` 现在带有三项稳定身份：

- `event_id`：同一语义回执跨 Bridge 重启和补发保持不变；
- `stream_id`：同一 workspace/session 的持久事件流身份；
- `cursor`：该 session 内单调递增的持久游标。

Bridge 会先把安全回执提交到 SQLite，再向 stdout 写 JSONL。即使提交后 stdout 断开，新的 Bridge
仍能根据业务回执的语义幂等键找回原 `event_id/stream_id/cursor`，不会把同一回执错误地登记成
另一条持久事件。

这是 ARC-02.5 的第一个可交付切片，不是完整断线重放。客户端 ACK、从 cursor 自动请求缺口、
slow-client snapshot 和活动运行恢复继续保留给 ARC-02.5b+。

## 权威边界

### 存储位置

Composition Root 创建唯一 `TerminalEventJournalStore`，默认路径为当前 Runtime 数据目录下的
`terminal-events.db`。使用项目默认配置时即：

```text
<workspace>/.naumi/terminal-events.db
```

`AgentEngine` 只暴露这一实例给 JSONL Bridge。Textual TUI 不经过 JSONL transport，不复制
Event Journal，也不伪造 cursor；两种前端仍共享原有 ChatRun/Harness 业务回执权威。

### 首批允许事件

ARC-02.5a 只允许：

- `completion/receipt`
- `harness/receipt`

进入日志。两者必须在共享协议注册表中同时满足：

- `criticality = terminal`
- `persistence = audit`
- `sensitive_fields = []`
- `redaction = none`

若注册表未来改变其中任何条件，Bridge 初始化直接失败，不能继续按旧假设写入。`ui/message`、
`engine/event`、错误详情、模型正文和工具输出均不会进入该数据库。

## Event Envelope

持久记录 schema version 为 1，包含：

| 字段 | 语义 |
|---|---|
| `event_id` | `tev_` 前缀的不可变事件身份 |
| `stream_id` | `tes_` 前缀的 workspace/session 流身份 |
| `cursor` | session 流内从 1 开始、永不回退的整数 |
| `event_type` | 允许持久化的协议事件类型 |
| `criticality` | 当前固定为 `terminal` |
| `idempotency_key` | 业务语义身份，不包含瞬时 request id |
| `payload_json` | canonical JSON，键排序且禁止 NaN |
| `payload_sha256` | payload 完整性摘要 |
| `envelope_sha256` | workspace/session、身份、cursor、类型、摘要和时间的联合摘要 |
| `occurred_at` | 首次持久提交时间 |

业务幂等键为：

```text
completion:<receipt_id>
harness:<run_id>:<revision>
```

resume 或显式 receipt resend 可以带新的 transport `request_id`，但仍复用原持久身份。相同幂等键
若对应不同 payload、类型或 criticality，Store 会 fail-closed，拒绝覆盖。

## 提交与并发顺序

1. Bridge 获取单 writer lock，解析当前协议策略与精确 session。
2. Store 以 `BEGIN IMMEDIATE` 开启事务。
3. 若幂等键存在，校验两层 SHA-256 后返回原记录。
4. 若不存在，在 workspace/session stream 上分配下一 cursor 并提交事件。
5. Bridge 使用返回的稳定身份构建 JSONL envelope。
6. stdout `write + flush` 成功后才推进进程内 `seq`。

SQLite 启用 WAL、5 秒 busy timeout，并对并发首次建库锁进行有界退避。多个 Store/Bridge 实例并发
追加同一 session 时，事务仍生成唯一且连续的 cursor。

### 写入成功、发送失败

持久提交发生在 stdout write 之前。发送失败不会删除已提交事件。下一 Bridge 使用同一幂等键补发
时取得原 cursor，而不是猜测前端是否收到。当前客户端尚未 ACK，因此 ARC-02.5a 不会自动决定
是否应补发；它只建立后续决策需要的事实。

## 保留策略

每个 workspace/session 默认保留最近 4096 条安全事件。清理旧事件不会回退
`terminal_event_streams.last_cursor`，后续 cursor 继续递增。ARC-02.5b 必须比较客户端 cursor 与
当前最早可用 cursor：

- 缺口仍在窗口内：按 cursor 重放；
- cursor 早于窗口：返回明确 gap，并要求 authoritative snapshot recovery；
- 禁止把窗口缺失误报为“没有新事件”。

由于 ARC-02.5a 尚未发布 replay request/response，这个 gap 行为只定义合同，尚未向用户宣称完成。

## 协议兼容

hello capability 新增可选 `terminal_event_cursor`。前端只接受
`completion/receipt`/`harness/receipt` 携带持久字段，并要求三字段同时存在、格式正确且 cursor
为正安全整数。其他事件携带这些字段会被拒绝。

进程内 `seq` 与持久 `cursor` 含义不同：

- `seq`：一次 Bridge stdout 生命周期内的连续帧校验；
- `cursor`：跨 Bridge 生命周期、按 workspace/session 持久化的安全事件位置。

重启后 `seq` 可重新从 1 开始，`cursor` 不会因此重置。

## 验收证据

- `test_terminal_event_journal.py`
  - Store 重建后幂等补发复用原身份；
  - 两个 Store 实例并发追加生成连续 cursor；
  - 幂等键冲突拒绝；
  - 数据库 payload 篡改被完整性校验发现；
  - 缩小保留窗口证明清理后 cursor 不复用；
  - 非 allowlist 事件和错误 criticality 被拒绝。
- `test_terminal_event_bridge.py`
  - 两个 Bridge 实例对同一回执给出相同稳定身份；
  - stdout 失败前已经完成持久提交；
  - 普通 UI 文本不被日志记录；
  - 回执没有 session 边界时不发送。
- `test_terminal_completion_receipt.py`
  - 真实 Git 修改、真实 pytest、真实 SQLite ChatRun 与真实 Engine 组合；
  - session resume 与显式 resend 得到相同 `event_id/stream_id/cursor`；
  - Journal 中的记录与 stdout envelope 相互对应。
- `protocol.test.js`
  - New UI 严格接受完整持久字段；
  - 缺字段、错误格式和错误事件类型均拒绝。

## 自我审视与剩余工作

- 已实现真正的持久事件身份、写前提交、并发 cursor、完整性校验、保留边界和协议验证，不是只在
  payload 上临时添加一个随机字段。
- 目前只覆盖无敏感字段的两类回执，尚未覆盖 tool result、permission、interaction、run progress
  或模型流；这些事件必须先分别确定脱敏、幂等和副作用合同。
- New UI 尚未按 `event_id/cursor` 去重，也没有发送 ACK。显式 resume 仍依赖 ChatRun/Harness
  Store 重建回执，再由 Journal 找回身份。
- 两层 SHA-256 用于发现损坏和不完整写入，不是抵御可同时重写数据库与摘要的本机恶意进程；
  Runtime Service 的本地用户隔离和文件权限仍属于 ARC-02.1/ARC-02.2。
- 保留窗口外的旧回执再次被用户主动请求时，当前会获得新的持久事件身份；ARC-02.5b 在加入
  ACK/gap/snapshot 时必须把窗口外语义明确化，不能声称无限期 exactly-once。
- 下一最小切片是 ARC-02.5b：客户端持久 ACK、`resume_after_cursor`、窗口内 resend 与窗口外
  gap/snapshot 信号。完成该切片后，HAR-07.4b 才能从“精确 session 重建”升级为“精确事件恢复”。
