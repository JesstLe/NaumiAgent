# EVO-05.3f2c3b2a Authenticated Worker Claim and Lease Fencing

## 目标

让 EVO-05.3f2c3b1 的 durable queued Dispatch 只能被它绑定的 exact remote Worker incarnation 领取，并以短期、可续期、
可动态失效的 lease 表达当前 transport ownership。该切片建立跨机器执行前的身份与领取边界，但不接收结果、不写 H5a，也不授予
execution、cohort、comparison 或 promotion authority。

## 为什么不能复用 Worker Contract digest

既有 `WorkerContract.contract_sha256` 和 `WorkerHealthReport.report_sha256` 能发现字段篡改，却不能证明远端调用者持有某个秘密，
因此不能作为网络认证凭据。本切片增加独立的 Ed25519 Worker Identity：

- 公钥绑定 `worker_id / instance_id / epoch / worker_contract_sha256`；
- enrollment 需要 control plane runtime key 生成 HMAC-SHA256 attestation；
- durable store 只保存公钥与 attestation，不请求或保存私钥；
- 同一 incarnation 只能绑定一个公钥，换钥必须注册更高 Worker epoch，因此旧 reservation 和旧 claim 同时被 fencing。

## 协议

### 1. Identity enrollment

`issue_evolution_revalidation_worker_identity()` 对 exact Worker Contract、公钥和 enrollment time 形成 canonical payload，使用
supervisor runtime key 签发 attestation。Service 在写入前重新读取 Worker Registry 的 current active registration，并拒绝旧 epoch、
错误 instance、错误 contract digest、无效 attestation 或早于 registration 的 enrollment。

### 2. One-time claim challenge

Control plane 只为 current Runtime Contract、active capacity reservation 和已 enrollment Identity 生成 challenge。签名 payload 使用独立
domain `naumi.evolution.revalidation-platform-claim.v1`，绑定：

- Dispatch / Job / Reservation identity 与 digest；
- Worker id / instance / epoch；
- Identity id / digest；
- 256-bit 随机 nonce、issued/expires、lease duration；
- action、claim sequence 与前序 receipt digest。

同一 Dispatch/action 只有一个未过期 pending challenge；过期 challenge 被机械标记为 expired 后才允许产生新 nonce。

### 3. Authenticated submit

Worker 使用本地 Ed25519 私钥签署 canonical payload。Service 验证时间窗、公钥签名、current Contract、active Worker incarnation、
Identity attestation 和 exact reservation；Store 在 `BEGIN IMMEDIATE` 中再次验证 Dispatch/Identity/challenge/receipt mapping、Ed25519
签名和 hash-chain sequence，然后原子写入 receipt 并关闭一次性 challenge。并发重复提交相同签名幂等返回同一 receipt，不同签名
不能复用已关闭 challenge。

### 4. Renewable fenced lease

首次 claim 形成 `sequence=lease_epoch=1`。续租必须对新的 action=renew challenge 重新签名，并绑定前一 receipt digest；每次成功形成
append-only receipt。当前 lease 同时受以下条件约束：

- receipt 尚未到期；
- Runtime Contract 仍 execution eligible；
- Dispatch 未变化；
- Worker Registry 仍指向 exact incarnation；
- capacity reservation 仍 active 且绑定 exact job；
- supervisor-attested Identity 仍匹配。

任一条件失败时历史 receipt 保留，但动态 view 变为 `stale` 或 `expired`，`worker_claimed` 与 `transport_delivered` 投影为 false。

## 状态边界

current receipt 仅证明 Worker 已认证取得 Dispatch 并拥有当前 transport lease：

- `worker_claimed=true`；
- `transport_delivered=true`；
- `execution_started=false`；
- `result_received=false`；
- `execution_authority=false`；
- `result_authority=false`；
- `cohort_authority=false`；
- `comparison_authority=false`；
- `promotion_authority=false`。

因此 UI、日志和后续聚合不得把 authenticated claim 显示为执行完成。

## 验收结果

- 使用真实 Ed25519 keypair 完成签名验证，错误私钥无法产生 receipt；
- 8 路并发 prepare/submit 只形成一个 challenge 和一个 sequence-1 receipt；
- signed renewal 形成前序 digest 链和递增 lease epoch，旧 lease 不再是 latest authority；
- challenge 和 lease 到期后分别 fail closed；
- 更高 Worker epoch 会 fencing reservation，并让旧 claim 动态 stale；
- 同一 Worker incarnation 无法替换 attested public key，持久化数据不含私钥；
- Engine composition、lazy public exports、聚焦 Ruff、10 个 Claim/Dispatch 测试与 import 通过；未运行全量测试。

## 下一切片

[EVO-05.3f2c3b2b1](EVO-05-3f2c3b2b1-claim-bound-execution-authorization.md) 已先将 current Claim 与父权限、Runtime lease、
可撤销 Run Grant 和 exact evaluation scope 绑定，但仍不接受自报 execution start。EVO-05.3f2c3b2b2 再增加内容寻址 result manifest、
artifact size/digest/provenance 校验和本地 H5a ingestion。只有本地验证器接受连续 cohort 后，Matrix lane 才能成为 completed。
