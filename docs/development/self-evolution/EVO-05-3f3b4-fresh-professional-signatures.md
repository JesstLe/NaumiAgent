# EVO-05.3f3b4 Fresh Professional Signatures

## 目标

为 Fresh Approval 中已选择 `approve` 的专业角色建立真实 Ed25519 身份与签名权威。长期 Principal 可以继续作为当前
专业身份/公钥登记，但每轮重新审批必须签署新的 challenge；旧 Response、旧 challenge、旧 signature receipt 和界面中的
角色声明均不能复用为本轮权威。

## 密码学绑定

`EvolutionRevalidationApprovalSignatureService` 使用独立 domain
`naumi.evolution.revalidation-approval-signature.v1`，签名 payload 同时绑定：

- canonical workspace、Fresh Requirement id/digest 与 Promotion Input id/digest；
- Fresh Response id/digest、Approval Request id、role 和 `approve`；
- Requirement `signable_payload_sha256`；
- current Principal id/event id/event digest；
- current Ed25519 key id/generation/public-key digest；
- 32-byte 随机 nonce、签发时间与不超过 Requirement 的到期时间。

私钥只留在签名方，Naumi 只公开 canonical payload 并验证 64-byte Ed25519 signature。challenge 和 receipt 都使用内容摘要
生成不可变 identity，并明确 `prior_signature_reused=false`。

## 动态与原子门禁

1. prepare 重新签发 current Fresh Requirement，重读该角色 Fresh Response 和 current Principal；
2. 仅 professional `approve` 且 Principal active/current/包含 exact role 时生成 challenge；
3. 并发 prepare 通过 singleflight 和 pending lookup 收敛为同一 challenge；
4. submit 首次复验全部来源，再以 current public key 验证真实签名；
5. 写入前第二次复验来源，并在 SQLite `BEGIN IMMEDIATE` 中同时核对 Requirement、Response、Principal current row；
6. 原子消费 challenge、写入唯一 receipt；并发重复提交同一签名幂等返回同一结果；
7. inspect 每次重新读取 current Requirement、Response 与 Principal，换钥、撤销或角色变更会撤销后续 aggregation 资格。

任何 wrong key、非 canonical Base64、到期 challenge、stale Requirement、非 approve Response、Principal 换钥/撤销/改角色或
事务内 authority 漂移均失败关闭。Receipt 只授予该角色进入下一步 Decision aggregation 的资格，不直接形成 Decision，不执行
promotion、Git、merge、push 或发布。

## 验收结果

- 使用真实生成的 Ed25519 keypair 完成 challenge、签名、验证和持久化；
- 8 路并发 prepare/submit 均收敛为一个 challenge 和一个 receipt；
- wrong-key signature 被拒绝；Principal 换钥后旧 challenge 被拒绝；
- receipt 绑定全新 Requirement/Response/Principal event，且旧签名不可复用；
- Engine composition root 与 `naumi_agent.evolution` lazy public export 已接入；
- 聚焦 Ruff、4 个签名/Response/Requirement 测试、import smoke 通过；未运行全量测试。

## 后续依赖

EVO-05.3f3b5 将聚合 user session consent 与所有 required professional Fresh Signature receipts，动态复验技术门禁和 current
authority，签发新的 Decision。其后才允许进入 staged rollout、运行监控、自动回滚和 outcome/feedback 回注。
