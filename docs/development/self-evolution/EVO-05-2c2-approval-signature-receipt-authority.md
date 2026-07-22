# EVO-05.2c2 Approval Signature Receipt Authority

## 目标与边界

本切片把 EVO-05.2b 的 `approve` Role Response 与 EVO-05.2c1 的 current active Approval Principal
连接成可验证的 Ed25519 Signature Receipt。它不是让 Naumi 持有签名密钥，而是提供严格的两阶段协议：

1. `prepare` 冻结 exact Response、Requirement、Package、role、Principal、key generation、随机 nonce 与有效期，
   返回 canonical bytes 的 Base64；
2. 私钥持有者在 Naumi 之外签署这些 bytes；
3. `submit` 只接收 64-byte Ed25519 signature 的 canonical Base64，重读全部 authority 后验证并持久化 Receipt。

本切片不收集、生成、传输或保存私钥，不聚合最终 quorum，不决定 approval，不执行 Promotion、Git、merge、
push 或 publish。最终非执行型审批聚合属于 EVO-05.2d。

## Domain-separated payload

签名域固定为 `naumi.evolution.approval-signature.v1`，策略版本固定为
`evolution-approval-signature-v1`。canonical JSON 使用 UTF-8、排序 key、无额外空白，payload 必须同时绑定：

- canonical workspace root；
- Approval Response ID/digest、Approval Request ID、固定 `approve` response；
- Requirement ID/digest、Package ID/digest 与 source signable payload digest；
- required role；
- Principal ID、exact Principal Event ID/digest；
- key ID、单调 key generation 与 public-key digest；
- 32-byte CSPRNG nonce；
- issued-at 与 expires-at。

Challenge 同时公开 canonical bytes 的 Base64 与 SHA-256。签署其他摘要、其他 domain、其他请求、旧 key、旧
generation 或仅签 Package digest 都不能通过。

## Challenge 生命周期

`EvolutionApprovalSignatureStore` 为每个 `(approval_response_id, principal_id)` 保留最多一个 active
Challenge，状态为：

`pending → consumed | expired | superseded`

- 默认 TTL 15 分钟，可配置范围 30..3600 秒，且永不超过 Requirement expiry；
- exact 并发 prepare single-flight 收敛到同一 Challenge，不产生多个 nonce；
- Principal key/role authority 变化后旧 Challenge 被 supersede，新 Challenge attempt 单调递增；
- consumed Challenge 只允许 exact signature 幂等重试，不同 signature fail closed；
- Requirement 剩余窗口不足 30 秒时拒绝创建 Challenge；
- 同一 Approval Response 成功后不再生成新的签名 Challenge。

Challenge 是持久 authority，不依赖进程内 callback 或模型上下文。数据库同时核对 artifact JSON 和 workspace、
response、requirement、role、Principal、event、key、attempt、expiry 等索引列。

## Receipt 与动态资格

`EvolutionApprovalSignatureReceipt` 嵌入完整 Challenge、exact Principal Event、public signature 与验证时间，并在
模型重载时再次执行 Ed25519 验证、artifact digest/ID 校验和时间窗口校验。固定边界字段明确声明：

- `signature_verified=true`；
- `identity_binding_verified=true`；
- `role_binding_verified=true`；
- `private_key_requested=false`、`private_key_stored=false`；
- `overall_approval_decided=false`、`final_quorum_reached=false`；
- `promotion_authority=false`、`promotion_executed=false`、`git_write_executed=false`；
- `llm_generated=false`。

历史 Receipt 不因 Principal 轮换或撤销而消失，但只读 Authority 每次都重读 current Requirement/target、Response
和 Principal。只有 Package/target/expiry、Response、Principal active state、exact key generation 与 role 全部仍
current，`eligible_for_future_aggregation` 才为 true。这样既保留审计事实，也不让旧签名进入未来聚合。

## 事务与并发边界

Receipt 写入在单个 `BEGIN IMMEDIATE` 事务内：

1. 重读 Response、Requirement 与 Principal latest-index；
2. 核对 exact Challenge JSON 与索引；
3. 拒绝关闭或过期 Challenge；
4. 插入唯一 Receipt；
5. 原子把 Challenge 标记为 consumed 并绑定 Receipt ID。

Response、Requirement 或 Principal authority 在验证与写入之间变化时，第二次 source resolve 与事务内 latest-index
核对都会阻断写入。数据库损坏、JSON/索引不一致、ID collision 或并发不同签名均 fail closed。

## 双通道与用户体验

Slash 命令：

```text
/evolution approval-signature prepare <approval-response-id> <principal-id>
/evolution approval-signature submit <challenge-id> <ed25519-signature-base64>
/evolution approval-signature show <receipt-id>
```

写入型 Agent Tool `evolution_approval_signature` 使用 `prepare|submit` action；只读 Tool
`evolution_approval_signature_authority` 只重载 Receipt 与 current eligibility。New UI 通过共享 Slash channel
进入同一 Engine service；Textual TUI 复用相同 slash router，没有第二套签名状态机。

Challenge 回执明确展示“在 Naumi 外部签署 exact Base64 解码 bytes”、payload digest、key generation、expiry 与
提交命令。任何输出都不要求用户粘贴私钥。

## 权限模型

写 Tool 是 `MEDIUM`，family 为 `evolution_approval_signature`，normal session 上限 50；
permissive/moderate/strict 允许且不做额外 PermissionChecker 确认，lockdown 阻断，bypass 全权限直接通过且不受
调用上限约束。密码学验证、workspace、expiry、current authority 和 Store 不变量在所有模式下都不可绕过。

## 验收证据

- 真实 `Ed25519PrivateKey` 在测试调用方签署 canonical bytes，Naumi 只持有 public key 与 public signature；
- 8 路 prepare 返回同一 nonce/Challenge，8 路 exact submit 返回同一 durable Receipt；
- wrong key、wrong domain/package digest、非 canonical/不同 signature、跨 workspace 与非 approve response 拒绝；
- key rotation 使旧 Challenge 失效并生成 attempt 2；revoke 后历史 Receipt 动态失去聚合资格；
- Challenge expiry、Requirement expiry、target/Package current gate 均 fail closed；
- Challenge/Receipt JSON 或 SQLite index 篡改在重载时拒绝；
- Slash、Agent Tool、只读 Authority、Engine composition、lazy export、PermissionRule 与 New UI shared Slash route
  具有聚焦测试；
- Ruff、py_compile、相关 Python/Node 小模块和真实本地 SQLite 场景通过；未运行全量测试。

## 自我审视与剩余边界

本切片证明“某个 current trusted Principal 的 exact Ed25519 key 在有效窗口内签署了 exact approve Response”，但
Principal 仍是工作区本地治理身份，不等于企业 SSO、硬件证书或远程 CA。Receipt 也只是一项角色证据，不能单独
授权发布。

EVO-05.2d 必须重读完整 Decision/technical gate、Approval Requirement、所有 required Role Response、current
Principal 与 eligible Signature Receipt，明确区分 `approved|rejected|changes_requested|pending|stale`，形成
不可执行的 Approval Decision Receipt。它仍不得直接执行 Git；EVO-05.3 才处理 target 变化后的 rebase/revalidate。
