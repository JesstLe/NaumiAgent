# HAR-10.7e Exact Fenced Agent Recovery Action

## 1. 目标

HAR-10.7d 已把 expired `running` Agent Job 作为 `recovery_required` 投影到 Agent Control，但当时只有
只读事实。HAR-10.7e 增加第一条真实恢复写路径：用户可把当前会话中、claim 已过期且无法证明终态的
`running` Job 精确收口为 `unknown`。

该动作不是 retry、cancel 或 takeover。它不会重新调用模型，不会删除任务/结果，不会替前端猜测副作用
是否发生；它只让 durable capacity 不再被一个已经失去 owner 的不确定 running 事实永久占用。

## 2. 范围

### 2.1 已实现

- `AgentJobStore.mark_recovery_unknown()` 在单个 `BEGIN IMMEDIATE` 事务内复验：
  - `job_id`；
  - `expected_request_sha256`；
  - 当前会话的 `expected_session_id_sha256`；
  - `expected_claim_epoch`；
  - `expected_latest_receipt_sha256`；
  - 当前状态仍为 `running` 且 claim expiry 已到期。
- 相同 fence 的并发/重复请求只有一次写入新 receipt，后续返回同一 `unknown` 事实且 `applied=false`。
- `SubAgentManager.resolve_recovery_unknown()` 是 Bridge 与 Textual TUI 共用的窄动作端口；错误只返回稳定、
  content-free code，不公开 Store 内容或 owner。
- JSONL 新增协商能力 `agent_recovery_actions`：
  - client：`agents/recovery/resolve_unknown`；
  - server：`agents/recovery/action_result`。
- New UI 与 Textual TUI 在当前会话的 `recovery_required` 条目上提供 `u` 动作；该显式按键即动作意图，
  不再叠加第二次高风险确认。
- 动作结果后刷新同一 Agent Control authority，并保留用户可见完成/冲突回执。

### 2.2 明确不实现

- 自动 retry 或模型重放；
- 将 `unknown` 强行改写为成功/失败；
- 对 live running、其他会话、expired pre-start claim 或 publication 执行本动作；
- Supervisor 自动裁决、独立 Agent Worker、跨主机 takeover；
- 删除、ack、retention 或 dead-letter。

## 3. 权威链

```text
Agent Control recovery descriptor
  -> explicit u action
  -> typed protocol / Textual direct port
  -> SubAgentManager.resolve_recovery_unknown()
  -> AgentJobStore BEGIN IMMEDIATE
  -> authenticate latest Job + compare five fences + verify expiry
  -> append unknown lifecycle receipt
  -> refresh Agent Control snapshot
```

前端字段只是乐观请求条件，不是写权限。Store 会从 SQLite 重新恢复并认证最新 Job；任一条件变化都拒绝
写入。因此用户在旧快照上按键、另一个 Runtime 已 renew/finish、epoch 已推进或会话不匹配时都不会错误
释放容量。

## 4. 动作合同

请求只包含：

- `session_id`：Bridge 先要求与当前 session 完全一致，Manager 再哈希后交给 Store；
- `job_id`；
- `request_sha256`；
- `receipt_sha256`；
- 正整数 `claim_epoch`。

成功回执只包含：action、job ID、accepted/applied、稳定 code、中文 message、`unknown` 状态、新 receipt
SHA-256 和原 claim epoch。拒绝回执不携带持久终态或 receipt，避免用错误响应探测其他会话事实。

## 5. Bypass 与安全语义

`bypass` 表示工具权限不再要求额外批准，因此 `u` 不弹第二次确认。但 bypass 不能绕过：

- 当前会话绑定；
- request/receipt digest；
- claim epoch；
- running 状态与 lease expiry；
- receipt chain/HMAC/AEAD 认证。

这些是事实一致性和并发 fencing，不是用户权限确认。

## 6. 失败与并发

- stale request/session/epoch/receipt、live lease 或非 running 状态统一返回 `recovery_fence_changed`；
- key、SQLite、认证或输入异常返回 `recovery_unavailable`，且不声称状态已改写；
- 两个 Runtime 同时提交同一完整 fence：一个 `applied=true`，另一个幂等读取同一 unknown receipt；
- New UI 在动作 pending 时不重复发送；即使客户端重复发送，Store 仍保证写入一次；
- 协议能力未协商时 New UI 不发送事件，并提示升级完整安装；
- Bridge 明文 session 检查与 Store session digest 检查形成两层边界。

## 7. 用户体验

- 仅当前会话的红色 `recovery_required` Job 显示“按 u 精确收口为 unknown”；
- live、其他会话、publication 与已 unknown 条目明确显示无可用人工动作；
- 动作过程显示“正在提交精确恢复裁决”；完成、幂等或冲突回执在刷新后仍保留；
- 文案明确说明不会自动重放模型、不会删除持久证据。

## 8. 聚焦验收

- 真实 SQLite：wrong request/session/epoch/receipt、live lease 全部拒绝；过期 running 可收口；
- 并发两次完全相同请求只追加一次 unknown receipt；
- Manager 精确哈希 session 并把所有 fence 原样传给 Store；冲突输出稳定低敏 code；
- Bridge 未协商能力时拒绝写事件，协商后产生 correlated typed result，跨 session 失败关闭；
- New UI 只发送一次五字段请求，协议 normalizer 丢弃额外字段并拒绝不可能的 result 组合；
- Textual TUI 按 `u` 直接调用共享 Manager 端口，无第二次确认，并在刷新后保留完成回执；
- 只运行 AgentJob、SubAgentManager、Bridge、Agent Control/TUI 与 New UI Agent Control 小模块测试。

## 9. 自我审视与后续依赖

本切片解决的是“未知副作用如何安全释放 capacity”，不是通用 Supervisor。仍未完成：

- expired pre-start claim 的自动 scheduler takeover；
- 独立 Agent Worker registration/transport/heartbeat/drain；
- publication retry budget、quarantine/dead-letter 与 retention；
- push stream、历史恢复动作审计页和 A5 kill/soak。

下一步应重新比较周期 publication retry、Agent Worker dispatch 与 ARC-06 scheduler owner lease。不要把
本动作扩张成“所有红色状态一键修复”，也不要让 Supervisor 绕过同一 exact fence。
