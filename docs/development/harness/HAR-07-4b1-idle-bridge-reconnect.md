# HAR-07.4b1 空闲 Bridge 进程重连与权威回执恢复

## 用户结果

New UI 的 Python Bridge 在没有活动运行、待确认权限、待处理交互或未确认发送时意外退出，终端
界面不再立即退出。它会在同一前端进程内有界重启 Bridge、重新执行 hello 协商，并使用断线前的
精确 `session_id` 发起替换式 `resume`。Bridge 随后继续复用 HAR-07.4a，从持久化 Harness Store
恢复 `harness/receipt`，再恢复同 run 的通用 `completion/receipt`；前端仍按 run id/revision 合并
为一张权威完成卡。

这是一项 HAR-07.4b 的进程级前置，不是完整的 cursor/revision/gap 恢复。Textual TUI 直接消费
embedded Engine，不经过 JSONL Bridge，因此本切片没有为 TUI 伪造“重连”状态；它继续使用共享
Store 和显式 `/resume`。

## 状态机

1. Bridge 退出时先停止心跳、丢弃未完成的流式合并帧并撤销旧协议协商权威。
2. 只有以下条件全部成立才进入自动恢复：
   - `state.running` 为 false；
   - 没有待裁决 permission；
   - 没有当前或排队 interaction；
   - 本地 outbox 没有 `queued` 状态的未确认消息。
   - 没有运行中的 Harness batch/cancel/promotion、Agent stop、Workbench proposal、Doctor export 或
     Evolution 写操作。
3. 恢复期间清除旧 Bridge 的 `protocol_registry` 证明，重置 server sequence guard；新进程必须
   重新完成 hello、capability 和 sequence-integrity 协商。
4. 最多尝试三次，间隔为 0/250/750ms。每个进程必须在默认 8 秒内完成 hello 与精确 session
   replay；超时会终止该进程并进入下一次尝试。测试可通过
   `NAUMI_BRIDGE_RECOVERY_TIMEOUT_MS` 缩短等待，生产值限制在 100ms..60s。
5. hello 成功后，恢复请求先于断线期间产生的 deferred sends，并固定携带
   `{session_id, clear: true}`；只有匹配本恢复 request id 的 `session/replayed` 才能结束恢复。
6. `session/replayed` 会清空旧会话的瞬态运行状态和 Harness 缓存；紧随其后的持久回执按既有
   revision 幂等规则重新填充。只有 Bridge 发出该 resume 末尾的 `runtime/status` 后才确认恢复并
   释放 deferred sends，不能把“刚开始 replay”误当成回执恢复完成。
7. 没有 session 的启动期空闲断线也可重连，但只在新 Bridge 发出 `ready` 后恢复输入。
8. 恢复后有 5 秒稳定窗口；窗口内再次退出继续消耗同一事故的三次尝试预算。只有稳定存活后才
   重置预算，避免“replay 一开始就崩溃”的进程形成无限重启循环。

## Fail-closed 边界

- 活动运行、permission、interaction、未确认发送或未结束控制操作存在时，New UI 以退出码 1
  停止，交由启动器 fallback 到 Textual TUI；不会重发 submit、tool、permission、interaction
  或 Harness/Workbench 写操作。
- hello 协商被拒绝、精确 session 恢复失败、回执链未完成或三次尝试耗尽时同样 fail-closed，
  并显示中文原因。
- 旧 Bridge 的 additive registry 证明不能跨进程继承；新 Bridge 在发布自己的 ready/status 前，
  未知 informational 事件不会借用旧证明被接受。
- 本切片不恢复活动模型流、工具调用或瞬态 progress。没有 Event Store/cursor 证据时，任何自动
  重放都会产生重复副作用风险。

## 验收证据

- `bridge-recovery.test.js` 覆盖空闲判定、活动/权限/交互/outbox 拒绝、三次重试上限、精确 resume
  关联、超时边界，以及重连时旧协议权威失效但回执暂存不被提前清除。
- `index-process.test.js` 使用会真实退出并生成第二个 OS 进程的 JSONL fixture：
  - 第一个 Bridge 发布 session 后以非零码退出；
  - New UI 保持同一进程，第二次 hello 从 seq=1 重新协商；
  - 前端只发送一次精确 resume；
  - 第二个 Bridge 依次发送 Harness 与通用回执；
  - 最新画面只出现一份“恢复后的权威回执”。
- 独立活动运行场景在 `run/started` 后杀死 Bridge，验证 New UI 退出码为 1、没有第二次 hello，
  且审计日志记录 `active_run`，证明没有静默自动重放。
- 聚焦 launcher 测试验证退出码 1 只触发一次 Textual fallback；fallback 自身失败时返回明确错误，
  不形成 UI 间无限重启。

## 自我审视与剩余工作

- 已实现“Bridge 进程能重启、重新协商并复用持久权威恢复空闲会话”，修复了此前 Bridge 一退出
  New UI 就结束的缺口。
- 尚未实现 ARC-02.5 Event Store、global cursor、client ACK、bounded resend buffer、slow-client
  policy 和 revision/gap 自动补发。因此 HAR-07.4b 仍为 partial。
- 活动运行透明恢复必须等 Runtime Service 能证明哪些事件和副作用已经持久提交；不能根据前端
  最后一帧推断，也不能把 submit 重发当作恢复。
- 后续最小 ARC 前置应定义 Event Envelope 的稳定 event id/cursor、持久写入顺序和 ACK/replay
  合同，再把本切片的精确 session resume 升级为 cursor resume。
