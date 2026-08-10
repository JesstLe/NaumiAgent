# ARC-04.1d Authenticated Worker Identity Authority

## 状态

已实现。

## 为什么需要独立前置

Worker Contract、Registry active pointer 与 durable Health Report 能证明“Runtime 当前登记了哪个进程”，但不能证明
远端 claim 请求确实由该进程持有的密钥签名。HAR-09.6c2a3c 若只比较 worker id/instance/epoch，任何能读取这些公开
字段的调用方都可以冒充 Worker。

旧 required-platform revalidation 内部已有领域专用的 identity 类型，但它的 policy、ID 和 durable table 都绑定旧
`evolution_revalidation` 协议。直接复用会把 post-rollback claim 绑定到错误领域；复制整个 claim 协议则会形成两套
密码学真相。本切片只抽取跨 Harness 所需的最小通用身份 authority，后续 claim 消费它。

## Artifact

`AuthenticatedWorkerIdentity` 绑定：

- exact `worker_id / instance_id / epoch`；
- exact Worker Contract SHA-256；
- Ed25519 public key 与 public-key SHA-256；
- enrollment timestamp；
- control-plane HMAC-SHA256 supervisor attestation；
- `claim_signature_eligible=true`。

artifact 明确保存 `private_key_stored=false`，且 execution/result/learning/promotion authority 全部为 false。
`claim_signature_eligible` 只表示该 public key 可用于验证后续 one-time challenge，不表示已经取得 claim lease。

## Enrollment 与动态 authority

`AuthenticatedWorkerIdentityAuthority.enroll()` 必须同时满足：

1. Worker Contract 自身摘要有效；
2. public key 是 canonical 32-byte Base64；
3. supervisor key 至少 32 bytes，且 HMAC attestation 匹配完整 identity core；
4. Registry 中存在 exact active incarnation；
5. enrollment 不早于 registration；
6. 同 incarnation 未绑定其他 public key。

持久化后重新读取 Registry 与 artifact。higher epoch takeover、revoke、contract 变化、HMAC key 不匹配或 durable
内容篡改都会动态撤销 `identity_authority`。

## 并发与换钥

- Store 使用 `BEGIN IMMEDIATE` 和 incarnation unique constraint；
- 两个独立 Store/Authority 并发登记相同事实时幂等收敛；
- 同 epoch 不允许替换 public key；合法换钥必须注册 higher Worker epoch，再签发新 identity；
- public identity 可关闭重开恢复，Worker private key 从不进入 SQLite、日志或 UI。

## 密钥读取边界

默认 supervisor key provider 复用 Runtime payload key，但构造 Store/Authority 时不会读取密钥。只有显式 enrollment、
inspect 或后续 claim verification 才调用 provider；因此普通启动、Python import 和无需身份认证的操作不会触发秘密读取。

## 验收证据

`tests/unit/test_authenticated_worker_identity.py` 使用真实 Ed25519 key、Worker Registry 与 SQLite 验证：

1. 双 Authority 并发 enrollment 幂等；
2. 关闭重开后 exact contract 可恢复同一 identity；
3. artifact 不含 private key；
4. higher epoch takeover 动态 fencing 旧 identity；
5. 同 epoch key reuse 冲突；
6. 错误 supervisor key、非 canonical Base64、损坏 Contract 摘要 fail closed；
7. enrollment 时间回退被拒绝；
8. SQLite artifact 篡改撤销 durable authority。

## 后续

HAR-09.6c2a3c 已使用本 identity 签发 one-time claim/renew challenge，验证 Ed25519 signature，并把 claim lease
expiry 同 exact capacity reservation expiry 取较早值。ARC-04.1e 又建立独立 X25519 Transport Key；签名 key 与
加密 key 不混用，二者都不能单独等同于 baseline transport 或执行授权。
