# ARC-04.1e Authenticated Worker Transport Key

## 状态

已实现。

## 目标

为远端 Worker 建立可轮换、可动态撤权的 X25519 public encryption key，使后续
`HAR-09.6c2a3d` 能把一次性 AES-256-GCM 内容密钥安全封装给 exact Worker incarnation。

`ARC-04.1d` 的 Ed25519 public key 只用于验证 Worker signature。Ed25519 不是密钥协商算法，不能直接用于
payload 加密；同机 `RuntimePayloadKey` 也不能复制到远端、命令参数、SQLite 或 UI。本切片因此建立独立
transport-key authority，而不是错误复用签名密钥或持久化共享明文 secret。

## Artifact

`AuthenticatedWorkerTransportKey` 绑定：

- exact Authenticated Worker Identity ID 与 SHA-256；
- exact Worker id、instance、epoch 与 Contract SHA-256；
- 单调 `key_generation`；
- canonical 32-byte X25519 public key 与 SHA-256；
- `x25519 + hkdf-sha256 + aes-256-gcm` 协议组合；
- enrollment timestamp；
- control-plane HMAC-SHA256 supervisor attestation。

artifact 明确保存 `private_key_stored=false`，并保持 delivery、execution、result authority 全部为 false。
`transport_encryption_eligible=true` 只表示后续可以向该 public key 封装一次性内容密钥，不表示已经传输任何
archive、manifest、baseline 或执行请求。

## Durable authority 与轮换

Identity 与 Transport Key 必须位于同一个 SQLite authority。Store 在 `BEGIN IMMEDIATE` 内重读 exact
Identity JSON，并执行：

1. 初始 generation 必须为 1；
2. 每次轮换必须恰好 `latest + 1`，不允许跳号或回退；
3. 新 generation 的 enrollment time 必须严格递增；
4. 同 identity/generation 相同 artifact 幂等，不同内容冲突；
5. X25519 public-key digest 全局唯一，禁止跨 identity/generation 复用。

Store 关闭重开后按 generation 恢复 latest key。旧 generation 保留审计事实，但 `latest_generation_valid=false`，
不能再用于新 envelope。

## 动态 fencing

enroll、resolve 与 inspect 均重新验证：

- supervisor attestation；
- exact durable Transport Key；
- exact durable Authenticated Worker Identity；
- Identity 的 active Worker incarnation authority；
- latest key generation；
- transport-key enrollment 不早于 identity enrollment。

higher Worker epoch、Worker revoke、Identity 篡改、Transport Key 篡改、supervisor key 不匹配或更新 generation
都会立即撤销旧 `transport_key_authority`。

## Runtime 边界

Composition Root 只构造 Store/Authority，不读取 supervisor key、不生成 X25519 private key，也不自动 enrollment。
private key 必须留在 Worker。该能力属于 supervisor/transport 内部安全原语，不新增用户 Slash 或 Agent Tool，避免
把密钥登记错误暴露为普通模型能力。

## 验收证据

`tests/unit/test_authenticated_worker_transport_key.py` 使用真实 X25519/Ed25519 key、Worker Registry、Identity 与
SQLite 验证：

1. 两个独立 Authority 并发 enrollment 幂等收敛；
2. artifact 不保存 X25519 private key；
3. 关闭重开恢复 latest generation；
4. generation 连续轮换并动态撤销旧 key；
5. higher Worker epoch fencing current key；
6. 错误 supervisor attestation、跳代、同代冲突与 Store split fail closed；
7. SQLite artifact 篡改撤销 authority；
8. X25519 低阶/全零 public key 在 enrollment 前被拒绝；
9. 默认 Runtime 构造不读取或生成 transport secret。

## 下一切片

`HAR-09.6c2a3d` 已消费 current Claim 与 current Transport Key，使用 ephemeral X25519 + HKDF-SHA256 派生
一次性 AES-256-GCM key，并在 exact Worker 解密、下载、验摘要和签署 ACK 后写 durable delivery receipt。本 key
artifact 仍永远不等于 transport delivery。`HAR-09.6c2a3e` 已由 Worker-signed Start challenge、父权限、Run Grant
与 Runtime lease 消费 current delivery；下一步接收该 exact authorization 下的签名结果。
