# ARC-04.5b2 Durable Agent Job Authority

## 1. 交付边界

ARC-04.5a 已签发低敏 request/result contract，ARC-04.5b1 已提供 Runtime key 与 authenticated
envelope。本切片建立独立 `AgentJobStore`，让 scheduler 可以在进程重启后恢复尚未开始的 Agent 请求，
同时不把 task、context、session ID 或 message topic 以明文写入 SQLite。

它交付 durable authority，不等于 Agent daemon 已完成。ARC-04.5c 已让 `SubAgentManager` 切换到该
Store；模型原始 response 仍未作为可恢复的加密 terminal payload 持久化。

## 2. 不可变请求与加密 payload

每条 Job 由两类事实组成，但内容敏感字段全部进入同一个 envelope：

- `AgentWorkerRequest`：只包含 task/context/session/topic digest、精确 tool scope、permission、model tier、
  turn/cost/timeout 限制和 `request_sha256`；完整 request JSON 同样加密，避免直接持久化
  `task_sha256/context_sha256` 形成低熵内容猜测 oracle；
- `PayloadEnvelope`：包含 task ID、session ID、task、context 和 message topic 的二进制有界 framing，
  由 AES-256-GCM 加密。

SQLite 主表只保留复合 `request_sha256`、幂等 request ID、状态与 envelope；不保存完整 request JSON。

二进制 framing 使用固定 magic、逐字段 32-bit 长度和 UTF-8 bytes，避免 JSON escaping 让 2MB task +
16MB context 突破不可预测的空间上限。Envelope 上限由 16MB 校正为 20MB，足以覆盖合同最大值和 framing。

AAD 精确使用 `request_sha256`。admit 前和解密后都机械复核：

- task/session/context/topic digest；
- task/context UTF-8 byte length；
- request 本身的结构与摘要。

因此替换 envelope、替换 request、wrong key、wrong AAD 或修改公开摘要都不能产生可派发 payload。
`AgentJobPayload.repr` 隐藏全部原始字段，Store 错误不会透传 key backend 或 cryptography 细节。

## 3. 生命周期与 claim fencing

状态机为：

```text
admitted -> claimed -> running -> completed|error|timeout|max_turns|cancelled
    |          |
    |          +-- expired before running -> claimed (new owner, epoch + 1)
    +-- cancel before claim -> cancelled

running + expired lease -> recovery review -> unknown
```

关键不变量：

- `claim_next()` 在 `BEGIN IMMEDIATE` 内按 `admitted_at, job_id` FIFO 选择；
- 同一 live claim 只有一个 owner，首次 claim 和 takeover 都单调增加 epoch；
- 只有 live owner + exact epoch 可以解密 payload、进入 running、renew 或提交终态；
- `claimed` 在真实模型调用前过期可安全 takeover；
- `running` 过期绝不自动重跑，因为模型调用可能已计费或产生外部副作用；
- expired running 进入 `list_recovery_required()`，只能用 latest receipt fence 收口为 `unknown`；
- terminal retry 只对同 owner、同 epoch、同 result 幂等，旧 owner 不因“已完成”绕过 fencing。

## 4. 防篡改事件链

每次 admission、claim、takeover、renew、running、terminal 或 recovery unknown 都追加
`AgentJobLifecycleReceipt`：

- sequence 与 previous state；
- owner、claim epoch、expiry；
- result digest（若存在）；
- transition digest、previous receipt digest 和 receipt digest；
- 使用 Runtime key 派生的 domain-separated HMAC-SHA256 authentication。

读取 Job 时会验证完整事件数量、连续 sequence、previous-state/receipt 链、latest pointer，以及主表中的
request/state/owner/epoch/result 是否与 latest receipt 一致。公开 digest 即使被攻击者重算，缺少 Runtime
key 也不能伪造 receipt authentication；事件或主表任一处被修改都 fail closed。

## 5. 运行时装配与跨平台状态

- `RuntimePaths.agent_job_db_path` 固定为 runtime data dir 下的 `agent-jobs.db`；
- `RuntimeResources` 和 Composition Root 始终构造惰性的 `AgentJobStore`；
- 构造 Store、启动 UI、运行 Doctor 或读取 Store Catalog 都不会解析 key 或触发系统钥匙串；
- 首次 AgentJob read/write/claim 才调用 Runtime key provider；凭据层继续复用进程缓存；
- Store Catalog 将其登记为 schema v1、restricted、audit-long-term；
- 新建目录/数据库在 POSIX 上分别收敛为 `0700`/`0600`，Windows 使用平台 ACL 语义。

## 6. 聚焦验收

- 真实 SQLite admit → close → reopen → claim → decrypt 恢复精确 payload；
- 数据库 bytes 不包含 task、context、session ID 或直接 task/context digest；
- 构造 Runtime Resources 不创建数据库、不访问 key；
- 相同请求幂等，相同 request ID 绑定不同 request/payload 被拒绝；
- 两个独立 Store 并发 claim 只有一个 owner；
- pre-start expiry 可 takeover 且 epoch 增长，旧 owner 无法 start；
- running expiry 不能 takeover，只能列入 recovery 并 fenced unknown；
- renew、terminal、terminal retry 和 wrong owner/epoch 均符合状态机；
- 重算公开 envelope digest 后修改 ciphertext 仍无法恢复；
- lifecycle event tamper 即使重算公开 transition/receipt digest，仍因 HMAC 无效而 fail closed；
- key provider 失败不泄露 backend 信息，也不留下半初始化数据库。

## 7. 自我审视与下一依赖

ARC-04.5c 已完成 admit → claim → recover → running → renew → finish 生产链，并建立 terminal
publication barrier；ARC-04.5d1 又把 response/error 加密绑定到 terminal result 并在生产发布前
重新认证恢复。仍未完成：

- 没有自动 scheduler loop、claim-owner heartbeat 或 capacity reservation；
- publication outbox、父进程崩溃后的自动结果重放与幂等消费回执；
- `unknown` 只有证据化收口，没有 Provider request ID 对账或人工恢复动作；
- key rotation、reencrypt、retention/GC、backup restore 和跨平台打包矩阵未完成。

下一步需比较 ARC-06 capacity queue、encrypted response recovery 与 Agent recovery UI 的依赖顺序，
仍不得把 embedded durable dispatch 宣称为长寿命 Agent daemon。见
`ARC-04-5c-embedded-agent-durable-dispatch.md`。
