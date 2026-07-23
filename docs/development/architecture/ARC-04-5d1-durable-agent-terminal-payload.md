# ARC-04.5d1 Durable Agent Terminal Payload

## 1. 用户问题与切片边界

ARC-04.5c 已建立“模型结束 → result receipt → durable terminal commit → 对外发布”的屏障，但
`AgentWorkerResult` 只保存 response/error 摘要。若 Runtime 在 terminal commit 后、父调用方或 UI
收到 response 前崩溃，Store 能证明任务成功，却无法恢复需要展示的原文。

本切片只完成 ARC-04.5d 的第一项安全前置：

```text
model result
  -> issue low-sensitivity result receipt
  -> bind exact response/error to receipt
  -> encrypt terminal payload
  -> atomically commit receipt + encrypted payload + terminal state
  -> reopen Store and authenticate payload
  -> publish only the recovered bytes
```

它不实现 publication outbox lease、跨 Runtime 自动重放、已读游标或恢复 UI，因此 ARC-04.5d 与
ARC-04 仍为 partial。

## 2. AgentJob schema v3

`AgentJobStore` 从 schema v2 迁移到 v3，在 `agent_jobs` 增加 nullable
`terminal_payload_envelope_json`：

- 新 terminal commit 必须写入 AES-256-GCM envelope；
- v1/v2 的既有 terminal Job 保持可读，缺少 payload 时明确报告不可恢复，不伪造空 response；
- admitted、claimed、running、pre-claim cancelled 与 recovery unknown 不写 terminal payload；
- v2→v3 只新增列，不改写 request envelope、capacity policy、result receipt 或 lifecycle chain；
- 重复迁移会先检查真实列集合，避免版本标记与物理 schema 已部分升级时重复 `ALTER TABLE`。

Store Catalog 使用 schema v3；生命周期 receipt 仍为 schema v1，因为状态机字段没有变化。

## 3. Terminal payload 合同与密文绑定

`AgentJobTerminalPayload` 只包含：

- `response`：UTF-8，最多 16 MiB；
- `error`：UTF-8，最多 1 MiB。

提交前机械验证：

- response 字节数等于 `AgentWorkerResult.response_bytes`；
- response SHA-256 等于 `response_sha256`；
- error SHA-256 等于 `error_sha256`；
- NUL、非法类型和超限输入在数据库事务前拒绝。

二进制 framing 使用固定 magic、显式 32-bit 长度和无尾随数据校验。AEAD AAD 同时绑定
`request_sha256` 与 `result_sha256`，因此不能把其他请求或其他终态的密文移植到当前 Job。数据库只保存
envelope JSON，不保存 response/error 明文、它们的直接路径或额外可枚举索引。

## 4. 原子终态提交与幂等

`finish()` 现在要求同时提供 result receipt 与 terminal payload。两者通过验证后，在同一个
`BEGIN IMMEDIATE` 生命周期事务中：

1. 复验 live owner、claim epoch、expiry 与 running 状态；
2. 签发 terminal lifecycle receipt；
3. 写入 result JSON；
4. 写入 terminal payload envelope；
5. 更新 terminal state。

任何一步失败都会回滚，不允许“terminal 已提交但 payload 未写入”的新记录。相同 owner/epoch、result 和
payload 的重放返回幂等 no-op；相同 result 搭配不同原文会 fail closed。

## 5. 认证恢复与生产发布屏障

`recover_terminal_payload(job_id, expected_result_sha256)` 要求调用方提供精确 result fence，并验证：

- Job 已进入有 result 的 terminal state；
- expected digest 与当前 result 一致；
- envelope 公开摘要、AEAD tag、AAD 和二进制 framing 全部有效；
- 解密后的 response/error 再次匹配 result receipt。

`SubAgentManager` 在 terminal commit 后立即通过该 API 重新读取 payload，并用恢复结果重建
`AgentResult`。因此 event callback、父委派返回值和 message bus 不再直接发布模型协程中的内存原文。
恢复连续失败两次时：

- response 置空；
- 返回“结果已安全隔离”的中文错误；
- Agent Control 保留 terminal job state 与稳定码
  `agent_job_terminal_payload_recovery_failed`；
- 已提交密文仍可由后续 outbox/recovery 模块处理。

## 6. 聚焦验收

- 真实 SQLite running → finish → close → reopen 可恢复精确中文 response/error；
- 数据库 bytes 不包含 response、error、task 或 context 明文；
- response/error 与 result digest 任一不匹配时，Job 保持 running；
- 修改 ciphertext 并重算公开 envelope digest仍因 AEAD 认证失败；
- wrong expected result digest 不能读取原文；
- 相同 terminal payload 重放幂等，不同 payload 重放被拒绝；
- v1→v3 与真实 v2→v3 迁移保留既有加密 Job；
- 生产 manager 正常路径返回持久层恢复结果；
- terminal commit 成功但恢复失败时不向调用方或 message bus 泄露内存原文；
- 只运行 AgentJob、SubAgentManager、Store Catalog 及直接依赖的小模块测试。

## 7. 自我审视与后续依赖

本切片解决“结果原文没有 durable source”这一根因，但还没有证明完整可靠发布：

- 没有 `pending/claimed/published` outbox 状态、publication lease 或 epoch fencing；
- Runtime 崩溃后不会自动扫描 terminal payload 并重放到父会话/UI；
- message bus 仍是进程内发布，尚无幂等 consumer receipt；
- 没有恢复结果的只读目录、人工发布动作、已读状态或 retention/GC；
- key rotation/reencrypt、跨平台打包崩溃矩阵和大结果 artifact 分层尚未完成。

下一步应实现独立的 `ARC-04.5d2 Agent Result Publication Outbox`，先建立可领取、可续租、可确认的发布
事实，再把只读 recovery UI 接到同一 authority；不要让 UI 直接扫描密文或自行推断“是否已发布”。
