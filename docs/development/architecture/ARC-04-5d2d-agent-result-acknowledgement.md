# ARC-04.5d2d Agent Result Acknowledgement

## 1. 用户问题与切片边界

ARC-04.5d2c 已让当前会话查看认证、脱敏的 durable result inbox，但结果始终显示为未读，用户无法留下
一个重启后仍可验证的消费事实。本切片增加精确的“标记已读”能力，并保持结果内容不可变：

```text
current session + delivery_id + delivery_sha256
  -> AgentJobStore schema v7 acknowledgement authority
  -> SubAgentManager shared action
  -> Agent Tool / slash command / Python Bridge
  -> New UI / Textual TUI
  -> refreshed Agent Control schema v7 projection
```

它只表达“这个本地 session 已确认看过这条精确 delivery”。它不是删除、归档、重试、消费游标、跨设备
同步、消息队列 ack 或外部 exactly-once 证明。

## 2. 持久化 authority 与不可变回执

`AgentJobStore.acknowledge_result_inbox_delivery()` 是唯一写入 authority：

1. 严格校验非空 session、受限 delivery ID 和小写 SHA-256 fence；
2. 在 `BEGIN IMMEDIATE` 事务中重新认证 delivery、publication、job、result 与 session routing HMAC；
3. 使用常量时间比较复验调用方提供的 `delivery_sha256` 与当前会话 routing HMAC；
4. 以 delivery identity 和 delivery digest 派生稳定 acknowledgement ID；
5. 首次调用追加一条 HMAC 认证回执，并发或重放返回同一条既有回执；
6. 不更新、不删除 result inbox、terminal envelope 或 publication receipt。

schema v7 新增 `agent_job_result_acknowledgements`。表由唯一 delivery、publication/job 外键与
`RESTRICT` 约束绑定；`no_update` / `no_delete` trigger 强制 append-only。回执包含
acknowledgement/delivery/publication/job identity、session routing HMAC、delivery digest、时间、receipt
digest 与独立 authentication digest。读取 inbox 时会逐条重新认证并验证绑定，篡改时 fail closed。

数据库不保存 session 明文，也不复制解密后的 task、response 或 error。参数化 SQL 负责所有值绑定；
错误消息不返回数据库路径、密钥、routing HMAC 或内部 receipt 内容。

## 3. 共享 Tool 与斜杠命令

Agent Tool `agent_result_acknowledge` 与用户命令
`/agents result ack <delivery-id> <delivery-sha256>` 复用同一
`SubAgentManager.acknowledge_result_inbox()`：

- Tool schema 禁止额外字段，并限制 ID 长度/字符与 SHA-256 格式；
- Tool 从 Engine 当前 session 取作用域，模型不能传入任意 session；
- metadata 标记为会改写状态、非破坏、并发安全、无需二次确认；
- CLI 通过已有 Tool slash 执行路径进入相同权限、审计与结果渲染；
- 成功回执明确说明加密结果没有被删除；fence/session 变化要求刷新后重试。

## 4. Agent Control、New UI 与 Textual TUI

Agent Control schema v7 在结果条目增加：

- `acknowledged`；
- `acknowledged_at`；
- `acknowledgement_receipt_sha256`。

summary 增加当前有界快照的 `durable_unread_results`。字段具有严格一致性：未读条目不能携带时间或
回执摘要，已读条目必须同时携带二者。摘要由后端认证事实计算，前端不自行推断。

New UI 通过协商后的 `agent_result_acknowledgement` capability 发送
`agents/result/acknowledge`，接收 `agents/result/acknowledgement` typed result；未协商 capability 的
旧客户端继续收到 schema v6 投影，新字段会被后端剥离。结果列表用不同颜色显示未读/已读，选中未读
结果后按 `v` 提交，pending 期间阻止重复发送，最终以刷新后的权威 snapshot 收口。

Textual TUI 同样在结果页使用 `v`，但通过 `Engine.execute_tool()` 调用共享 Tool；两端都显示确认时间、
回执摘要和“结果仍保留”的成功文案。`/agents result ack ...` 在 Textual 输入栏继续转发到共享 CLI
slash 路径。

## 5. 错误、并发与恢复语义

- delivery 不存在、跨 session、digest 变化或绑定不一致：拒绝写入，返回稳定失败码；
- 数据库、密钥或认证读取不可用：fail closed，不修改 result 或 acknowledgement；
- 同一 delivery 并发确认：只产生一条回执，其余调用返回 `result_already_acknowledged`；
- 进程重启：已读事实从 schema v7 Store 恢复，result 仍可按原 delivery fence 解密恢复；
- UI 网络/Bridge 回执丢失：用户可安全重放，最终 snapshot 是权威状态；
- receipt 或行数据篡改：后续 inbox 读取失败，不把不可信数据投影给 UI。

## 6. 聚焦验收标准

- Store schema v1-v6 均可迁移到 v7，新库直接创建完整 v7 schema；
- 真实加密 Agent terminal -> publication -> delivery -> acknowledgement 链路可运行；
- 错误 session、错误 digest、非法 ID、未知字段和篡改均被拒绝；
- 并发确认只追加一条认证回执，重放返回相同 acknowledgement identity；
- 重启后已读状态仍在，原始持久结果仍能恢复，数据库没有 session/response 明文；
- Agent Tool 与 CLI 使用相同 manager authority；
- New UI 只在 capability 协商后发送 typed action，旧客户端保持 schema v6；
- Textual TUI 使用共享 Tool，不直接写 Store；
- 两端都以 authoritative refresh 展示已读、时间和 receipt digest；
- 仅运行 Store、Agent Control、Tool/Slash、Bridge/New UI、Textual 的聚焦测试，不运行全量测试。

## 7. 自我审视与未完成边界

本切片真实完成了持久、认证、并发幂等的单条结果已读确认，没有通过前端临时状态或 prompt 模拟。但它
仍有明确边界：

- acknowledgement 是单一本地 session 事实，没有用户/设备 identity 或 per-consumer cursor；
- 没有批量已读、全部已读、分页、搜索、归档、删除、retention 或 GC；
- 没有将 receipt 锚定到外部透明日志或远端副本；
- 当前 UI 只显示有界结果摘录，完整大输出分页仍未实现；
- 独立 Worker 的结果消费与跨主机 sink 仍不能声明 exactly-once。

下一切片必须重新比较 Harness、ARC-06、UI 与自进化闭环的依赖，只实现能解锁下一项用户能力的最小
前置；不因本切片属于 ARC-04 就顺序做完整个 daemon/Supervisor 路线。
