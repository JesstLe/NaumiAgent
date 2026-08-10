# HAR-09.6c2a3c Authenticated Worker Claim 与 Fenced Lease

## 状态

已实现。

## 目标

让 `HAR-09.6c2a3b` 的 queued Remote Dispatch 只能被 exact Worker incarnation 领取，并签发短期、可续、
不可复活的 claim lease。公开的 worker id/instance/epoch、健康报告或 capacity reservation 都不是认证凭据；
Worker 必须持有 `ARC-04.1d` 登记 public key 对应的 Ed25519 private key。

## 协议

```text
current queued Dispatch + active reservation
  -> current supervisor-attested Worker Identity
  -> one-time random challenge
  -> Worker signs canonical payload with private Ed25519 key
  -> atomic signature/lineage/sequence verification
  -> claimed receipt with lease epoch
  -> optional one-time renewal challenge
```

challenge canonical payload 绑定：

- domain 与 action（`claim` / `renew`）；
- cryptographic random nonce；
- exact Dispatch digest、Job 与 reservation；
- exact Worker id/instance/epoch；
- exact Authenticated Worker Identity ID/digest；
- claim sequence 与 previous receipt digest；
- issued/expiry 与 requested lease seconds。

`signable_payload_sha256` 允许 Worker 在签名前核对收到的 canonical bytes。challenge 一次性关闭；相同 signature
重放幂等返回同一 receipt，不同 signature 或不同 claimed time 不能复用已关闭 challenge。

## Authority 重验

准备 challenge、提交 signature、renewal 和 inspect 均重新验证：

1. Remote Dispatch durable/current authority；
2. latest durable Worker Health 仍 healthy 且 accepting；
3. exact active Worker Contract；
4. exact active capacity reservation；
5. current supervisor-attested Worker Identity；
6. challenge/receipt 与 Dispatch、Identity、sequence chain 完全一致。

pending challenge 复用前也必须重验以上 authority，不能因 challenge 尚未过期而绕过新的 draining heartbeat、
higher epoch takeover 或 reservation fencing。

## SQLite 原子边界

Dispatch、Authenticated Identity、Claim challenge 与 Claim receipt 必须位于同一个 SQLite authority。Service
构造时强制路径一致；Store 在 `BEGIN IMMEDIATE` 内重读 exact Dispatch/Identity rows、关闭 one-time challenge、
验证 Ed25519 signature 与 previous receipt chain，再插入 receipt。

Worker Registry 位于独立 durable store，因此跨库不宣称事务原子性；每次提交后和每次消费前都重新 inspect，
任一侧变化立即撤销 claim view。

## Lease 与 reservation deadline

- challenge TTL 为 1..120 秒；
- claim lease 为 1..300 秒；
- `lease_expires_at = min(claimed_at + requested lease, original reservation deadline)`；
- renewal challenge 必须在 current lease 内完成；
- renewal sequence 与 lease epoch 单调递增；
- renewal 不能延长原 Dispatch 的 reservation deadline；
- old lease 到期、sequence 被推进或 reservation 到期后都不能复活。

这允许执行阶段使用较短 owner lease 做故障检测，同时禁止靠无限 renewal 永久占用 Worker capacity。

## 明确未授予

Claim receipt 只把 `worker_claimed=true` 与 `capacity_reserved=true` 投影为 current：

- `transport_delivered=false`；
- `execution_authority=false`；
- `result_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

claim 不下载、不解密、不安装或传输 target baseline，不运行 Eval，也不接受结果。

## 双通道入口

- Agent Tool：`evolution_post_rollback_remote_claim`；
- CLI/TUI/New UI 共享 Slash：
  - `/evolution outcome-claim-behavior prepare <dispatch-id>`；
  - `/evolution outcome-claim-behavior submit <challenge-id> <signature-base64>`；
  - `/evolution outcome-claim-behavior renew <claim-id>`。

prepare 输出 canonical JSON payload 与 signable digest。signature 是公开验证材料，不是 Worker private key；private key
不得进入命令、SQLite、日志或 UI。所有权限模式都不做高风险二次确认，但不能跳过密码学与 durable fencing。

## 验收证据

`tests/unit/test_post_rollback_remote_lane_placements.py` 使用真实 Dispatch、Registry、Health、Identity、Ed25519
private/public key 与 SQLite 验证：

1. prepare 幂等返回同一 pending challenge；
2. Tool、Slash、moderate/bypass 共享同一 Service；
3. signature 成功签发 sequence 1 lease；
4. 相同 signature 重放幂等，不同 signature 被拒；
5. forged Ed25519 key 被拒；
6. renewal receipt 绑定 previous digest，sequence/lease epoch 单调；
7. lease 被 original reservation deadline 截断；
8. challenge 不得晚于 reservation/current lease；
9. higher Worker epoch 动态 fencing claim；
10. expired lease 不再投影 worker claimed；
11. pending challenge 复用时重新验证 draining Health；
12. Dispatch/Identity/Claim store 路径不一致时构造即失败。

## 下一切片

`HAR-09.6c2a3d` 应建立 baseline transport delivery receipt：绑定 current Claim lease、target Build Attestation、
archive/manifest digest、加密 payload envelope 与 Worker ACK。只有 delivery current 后，后续 execution authorization
才可签发；claim receipt 本身永远不能直接启动 Eval。
