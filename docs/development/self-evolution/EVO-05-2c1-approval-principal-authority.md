# EVO-05.2c1 Approval Principal & Public-Key Authority

## 目标与边界

本切片建立 EVO-05.2c 签名验证所需的可信根：经 HAR-10.6 真实用户确认的 Approval Principal、角色绑定与
Ed25519 公钥生命周期。用户可通过 `/evolution approval-principal ...`，Agent 写动作可通过
`evolution_approval_principal`，只读查询通过 `evolution_approval_principal_authority`；三者进入同一
`EvolutionApprovalPrincipalService`。

本切片只接受 32-byte Ed25519 **公钥**的 canonical standard Base64。Naumi 不生成、不请求、不传输、不保存
私钥，也不接受助记词、密码或自定义审批文本。Principal authority 不等于角色审批响应，不产生 quorum、最终
approval decision、Promotion、Git、merge、push 或 publish 权限。密码学签名收集与验证现已由独立
[EVO-05.2c2](EVO-05-2c2-approval-signature-receipt-authority.md) 交付。

## Authority 模型

每个 Principal 由 `(canonical workspace root, normalized principal name)` 确定 stable identity：

`evprincipal_<sha256(policy, workspace, name)[:24]>`

每次治理动作形成 append-only `EvolutionApprovalPrincipalEvent`：

`register → rotate_key | update_roles → ... → revoke`

- `register`：创建 sequence 1、key generation 1 的 active Principal；
- `rotate_key`：角色不变，公钥必须变化，key generation 单调递增；
- `update_roles`：公钥和 generation 不变，角色集合必须变化；
- `revoke`：冻结最后角色/公钥并永久进入 revoked；已撤销 Principal 不能恢复、轮换或改角色；
- exact 重试返回同一 authority，不重复写 Event；不同内容不能覆盖已有 identity。

角色只允许 `user`、`independent_reviewer`、`security_reviewer`、`data_owner`、`release_manager`，去重后按策略
顺序冻结。系统不根据模型声明、用户名、Prompt 或已有 Approval Response 自动授予角色。

## HAR 治理交互

所有写动作都先创建 durable interaction：

`ask-evprincipal-<principal-suffix>-<action>-<attempt>`

请求显示 exact Principal、角色和 Ed25519 公钥 SHA-256 指纹，只允许 `approve` 或 `reject`：

- create-before-display、answer-before-authority-write；
- pending 不重复创建；cancelled 后 attempt 递增；
- answered proposal 可在进程重启后重读并消费；
- 同一 exact proposal 多个 answered authority 时 fail closed；
- Event 写入前从 Harness Store 重读 exact terminal interaction；
- `answered_by` 必须是 `user`，且必须绑定非空 session；Agent 不能代答；
- callback 返回值本身不是 authority。

`bypass` 跳过 PermissionChecker 的高风险确认，但不能自动回答 HAR，也不能绕过 exact proposal、actor、event
chain 或 Store 校验。normal 模式把该 Tool 标记为 high risk；HAR 仍是最终主体治理事实来源。

## 公钥与事件完整性

- `cryptography` 作为直接运行依赖解析并验证 Ed25519 public bytes；
- Base64 必须 canonical，解码后必须恰好 32 bytes；
- `public_key_sha256`、generation 与 Principal identity 共同派生 stable `key_id`；
- Event 包含 previous-event SHA-256，完整 JSON 使用 canonical SHA-256 和 stable event ID；
- SQLite 同时保存 append-only Event chain 和 latest snapshot；读取时重放全链并核对每个索引列；
- snapshot、Event JSON、sequence、key generation、hash link、roles 或索引任一不一致即 fail closed；
- Event 明确固定 `private_key_stored=false`、`private_key_requested=false`、
  `approval_decision_authority=false`、`promotion_authority=false`、`git_write_executed=false`。

## 双通道与前端

Slash 命令：

```text
/evolution approval-principal register <name> <ed25519-public-key-base64> <role[,role...]>
/evolution approval-principal rotate <principal-id> <ed25519-public-key-base64>
/evolution approval-principal roles <principal-id> <role[,role...]>
/evolution approval-principal revoke <principal-id>
/evolution approval-principal show <principal-id>
```

写入型 Agent Tool 使用 `register|rotate_key|update_roles|revoke` action；只读 Authority Tool 按 Principal ID
查询，不触发高风险确认或 HAR 交互。New UI 把该命令放入共享 Slash channel，
治理选项由现有 HAR interaction card 显示；Textual TUI 复用同一 slash router、Engine service 和 durable
interaction authority，没有第二套 Principal 状态机。

权限 family 为 `evolution_approval_identity`，每 normal session 最多 20 次，风险 high；lockdown 阻断，bypass
全权限通过且不触发 PermissionChecker 确认，但 HAR 人工 gate 保留。

## 验收证据

- 真实 Ed25519 keypair、SQLite 与 `DurableInteractionAuthorityClient`；私钥仅由测试调用方持有，数据库中不存在
  private raw/base64 bytes；
- 8 路并发 register 只创建一个 HAR interaction 和一个 Event，其余返回幂等结果；
- register、rotate、roles、revoke 形成 sequence 1..4 与正确 key generation/hash chain；
- revoked 后签名验证资格关闭，后续 rotate/update fail closed，重复 revoke 不重复写；
- reject 不创建 Principal；pending 不重复显示；cancelled 后单调 retry；跨 workspace 不泄漏；
- 无效 Base64/长度、Event 固定字段伪造、snapshot/index 篡改均拒绝；
- Slash、Agent Tool、Engine composition、lazy exports、PermissionRule 与 New UI shared Slash route 有聚焦测试；
- Ruff、py_compile、相关 Python 小模块与 New UI state 小模块通过；未运行全量测试。

## 自我审视与剩余边界

本 authority 自身只证明“本地用户明确把角色和某个公钥绑定到 Principal”。Principal name 仍不是企业 SSO、
证书链或远程身份提供商。EVO-05.2c2 已在此可信根上交付：

1. 定义 domain-separated canonical signable payload，绑定 Requirement、Package、role、response、Principal、
   key ID、nonce 与 expiry；
2. 用当前 active Principal 公钥验证真实 Ed25519 signature，不保存私钥；
3. 拒绝旧 generation、撤销后签名、跨 workspace/role/request 重放和过期 signature；
4. 形成 append-only Identity/Signature Receipt，并动态重读 Principal 与 Approval Requirement authority；
5. 仍不聚合最终决定；下一步 EVO-05.2d 重读所有技术门、角色响应、身份与签名回执形成非 Git 执行型 Decision。
