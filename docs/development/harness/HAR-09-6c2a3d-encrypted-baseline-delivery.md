# HAR-09.6c2a3d 加密 Baseline Delivery 与 Worker ACK

## 状态

已实现。

## 目标

让 `HAR-09.6c2a3c` 的 current Remote Claim 在 exact `ARC-04.1e` Transport Key 上建立一次性加密 baseline
descriptor，并且只有 exact Worker Identity 对“已解密、已下载、已验 archive/manifest”的 ACK 声明签名后，
才形成 durable `transport_delivered=true` receipt。

本切片不把 queued、claim、生成 ciphertext、控制面下载成功或网络写入成功冒充 Worker 已收到 baseline。

## 为什么传输 descriptor 而不是复制 archive

Release Channel artifact 最大可达 8 GiB。将 archive 放进 SQLite、ToolResult、模型上下文或单块 AEAD 会导致内存、
上下文和恢复风险。协议采用两段式：

1. 控制面复用 `ReleaseArtifactFetchService`，从 pinned origins 有界流式下载并验证 exact size/SHA-256；
2. 加密 descriptor 携带 signed Catalog Resolution、Build Attestation、pinned origins、archive/manifest digest；
3. Worker 解密 descriptor 后从同一 pinned origins 流式下载；
4. Worker 验证 archive size/SHA-256 与 manifest SHA-256 后签署 ACK。

因此，密文保持有界，而 Worker ACK 仍绑定真实 target artifact，不把 URL 字符串本身当作 delivery。

## 加密协议

```text
current Claim + current X25519 Transport Key
  -> ephemeral X25519 key pair
  -> X25519 shared secret
  -> HKDF-SHA256 one-time AES key
  -> AES-256-GCM encrypted descriptor
  -> Worker decrypts with private Transport Key
  -> Worker downloads and verifies target archive/manifest
  -> Ed25519 Identity signs fixed ACK payload
  -> atomic durable delivery receipt
```

每个 offer 使用新的 ephemeral X25519 key、32-byte HKDF salt、12-byte AES-GCM nonce 与 32-byte ACK nonce。
ephemeral private key、shared secret 和 AES key 不写入 artifact、SQLite、日志或 UI。

AAD 绑定 Delivery/descriptor、Dispatch/Claim receipt、Transport Key、archive/manifest、expiry、ephemeral public
key 与 plaintext size。wrong Worker private key、ciphertext/AAD 篡改或 lineage 不一致均无法解密。

## ACK 语义

固定 `AckPayload` 声明 descriptor 已解密、archive 已下载、archive/manifest digest 已验证，同时 installation 与
evaluation 尚未执行。Worker 使用 `ARC-04.1d` Ed25519 private key 对 canonical payload 签名；ACK nonce、
envelope/ciphertext/descriptor digest、Worker incarnation 与 artifact facts 全部进入签名输入。

密码学证明“exact Worker Identity 签署了该声明”；它不能防止已被攻陷的 Worker 主动撒谎。后续 sandbox、执行
receipt 和结果证据仍需提供独立交叉验证。

## SQLite 原子边界

Delivery Store 与 Claim、Dispatch、Target Baseline、Identity、Transport Key 共用 session SQLite。写 offer 或 ACK
receipt 时在 `BEGIN IMMEDIATE` 内重读 exact Dispatch、latest Claim、exact Identity、latest Transport Key 与 exact
Target Baseline。ACK signature verification、receipt insert 与 `awaiting_ack -> delivered` 在同一事务完成。

相同 signature 重放幂等返回同一 receipt，不同 ACK 不能关闭已完成 offer。Release Catalog/Download authority 位于
独立 release SQLite 与 immutable file，因此不声称跨库事务原子性；Service 在 prepare、ACK 和 inspect 边界重复验证。

## 动态 fencing 与重放

- offer expiry 不得超过 exact Claim lease expiry；
- 控制面下载完成后必须用新 clock value 重验 Claim/Download，不能用下载开始时的旧时间签发 offer；
- ACK 时间必须满足 `offer.issued_at <= acknowledged_at < offer.expires_at`，拒绝回拨或迟到；
- Claim renewal 会产生新 receipt，旧 offer 立即 stale；
- Transport Key rotation 会撤销旧 generation 的 offer；
- higher Worker epoch、Health/Reservation fencing、Catalog rotation、archive 篡改均撤权；
- 两个独立 Service 并发 prepare 使用稳定 Delivery ID，SQLite 选择首个 envelope 并让调用方收敛；
- offer 过期后必须先取得新 Claim receipt，不能复活旧密文。

## 权威边界

有效 Worker ACK receipt 只建立 `transport_delivered=true`。installation、execution、result、learning 与 promotion
authority 仍固定为 false。

## 双通道入口

- Agent Tool：`evolution_post_rollback_remote_delivery`；
- CLI/TUI/New UI 共享 Slash：
  - `/evolution outcome-deliver-behavior prepare <claim-id>`；
  - `/evolution outcome-deliver-behavior submit <delivery-id> <worker-signature-base64>`；
  - `/evolution outcome-deliver-behavior inspect <delivery-id>`。

prepare 返回 Worker 可消费的 canonical offer JSON 和 ACK signable digest。所有权限模式均不做高风险二次确认，
但不能跳过下载、密码学或 durable fencing。

当前协议对象通过 Tool/Slash 输出交给 Worker；自动远端 daemon push、重试队列、流量整形与大规模 fan-out 尚未接入。
因此，本切片完成可验证 delivery protocol，不宣称生产级自动传输集群已经完成。

## 验收证据

`tests/unit/test_post_rollback_remote_deliveries.py` 使用真实 signed Catalog/Build Attestation、流式 artifact fetch、
Ed25519、X25519、HKDF、AES-GCM、Worker Registry、Claim 与 SQLite 验证：

1. exact target archive 先由控制面有界下载并验证；
2. Worker private X25519 key 可解密，wrong key 与 ciphertext 篡改均不可解密；
3. descriptor 绑定 pinned origins、Build Attestation、archive/manifest；
4. forged ACK signature 被拒；
5. exact ACK 原子形成 delivery receipt，重放幂等且无执行权；
6. offer expiry 与 archive 篡改 fail closed；
7. 两个独立 Service 并发 prepare 收敛到一个 offer；
8. Claim renewal 与 Transport Key rotation 动态 fencing 旧 offer；
9. 下载跨过 Claim expiry 与 ACK 时间回拨均 fail closed；
10. Store authority split 在构造期失败；
11. Agent Tool、Slash、moderate/bypass 复用同一 Service 且无二次确认；
12. 默认 Runtime 构造不联网、不下载、不生成 Worker private key。

## 下一切片

`HAR-09.6c2a3e` 已建立 execution authorization：绑定 current Delivery receipt、current Claim lease、Eval Suite、
resource budget、attempt identity、父权限、Run Grant 与 start deadline。Worker 必须在执行前再次签署 start challenge；
delivery receipt 本身仍永远不能直接启动 Eval。6c2a3f 已接收 exact authorization 下的签名结果；下一步由 6c2b 聚合矩阵。
