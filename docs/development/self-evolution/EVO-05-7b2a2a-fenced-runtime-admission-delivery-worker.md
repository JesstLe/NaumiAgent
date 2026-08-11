# EVO-05.7b2a2a Fenced Runtime Admission Delivery Worker

## 目标与切片边界

[EVO-05.7b2a1](EVO-05-7b2a1-stable-promotion-runtime-admission-signed-delivery.md)
已经形成 installation-signed Submission 和 Control Plane Receipt，但 outbound 仍只能人工 export/receive。本切片把 durable
outbound 收口为 owner/epoch/lease fenced 的周期 Worker，并提供真实的 in-process Control Plane transport adapter。

本切片刻意不把“有 transport Protocol”描述成“生产网络已上线”。mTLS HTTPS、endpoint、证书 pin、请求大小和限流属于下一独立
切片 `EVO-05.7b2a2b`。因此 production composition 未配置 transport 时不创建 Worker，Tool/Slash 会明确失败关闭。

## Authority 分层

Worker 只自动化签名 Admission 的投递，不产生新的观察事实：

1. sender 在每次网络调用前重新执行 7b2a1 `prepare()`，动态验证 Admission、Contract、Population Finalization、member、current
   Credential 和既有 installation signature；
2. dispatch Store 逐次重读 exact 7b2a1 durable outbound，拒绝 source 删除、替换或行身份漂移；
3. authenticated transport 返回完整 7b2a1 Control Plane Receipt；
4. Worker 只在 live owner/epoch/lease 内持久化 exact Receipt；
5. Receipt 仍保持 Window、long-term metrics、Promoted Outcome、learning、promotion 和 execution authority 为 false。

`LocalStablePromotionRuntimeAdmissionControlPlaneTransport` 是真实接收适配器：它调用 Control Plane 7b2a1 Service，完成 current
Credential、signature、Contract/Finalization/member 和 durable Receipt 校验，并拒绝 stale View。它用于嵌入式部署与端到端验证，
不是网络模拟或 prompt wrapper。

## Durable 状态机

```text
queued
  -> in_flight(owner_sha256, epoch + 1, attempt + 1, lease)
     -> acknowledged(exact Control Plane Receipt)
     -> queued(failure code, exponential retry time)
     -> dead_letter(permanent failure or exhausted budget)

expired in_flight
  -> in_flight(new owner, epoch + 1, attempt + 1, new lease)
```

每个 `EvolutionStablePromotionRuntimeAdmissionDispatchEvent` 是 canonical JSON SHA-256 内容寻址，绑定 Admission ID、Submission ID/
digest、sequence、previous event digest、claim epoch、attempt、时间和可选 Receipt/failure。主记录与 append-only event chain 每次读取
都交叉验证。

### Fencing 不变量

- SQLite `BEGIN IMMEDIATE` 串行化 enqueue、claim 和 settlement；
- owner 原文只存在 Worker 内存，durable event 只保存 SHA-256；
- retry、dead-letter 和 ACK 必须同时匹配 current owner digest、claim epoch 与未过期 lease；
- 过期 owner 即使持有旧 Receipt 也不能 settlement；
- expired claim 可由新 owner 接管，并同时递增 epoch 与 attempt；
- acknowledged/dead-letter 均不再进入 claim 扫描；
- 同一 Admission 并发 enqueue 收敛到同一 Submission，different Submission 冲突关闭。

## Worker 策略与失败分类

`harness.stable_promotion_runtime_admission_delivery` 配置：

- `enabled`；
- `interval_seconds`；
- `max_empty_backoff_seconds`；
- `max_failure_backoff_seconds`；
- `claim_lease_seconds`；
- `scan_limit`；
- `receipt_timeout_seconds`，必须严格小于 claim lease；
- `retry_base_seconds` / `retry_max_seconds`；
- `max_attempts`；
- `shutdown_drain_seconds`；
- `jitter_ratio`。

timeout、临时 I/O 和明确 retryable transport failure 进入 durable backoff。signature/lineage conflict、不同 Receipt、durable
outbound/chain corruption 和 transport 标记的 permanent failure 进入 dead-letter。failure settlement 自身失败单独计数，不能把失败
误报成 ACK。

Worker Snapshot 暴露 pass、claim、ACK、retry、dead-letter、failure、forced shutdown、连续空轮/失败轮、下一 delay 与最近稳定 failure
code。start/wake/stop 由 Engine lifecycle 管理；shutdown 先 wake 并等待当前 pass 在 drain deadline 内结束，超时才取消。

## 双通道与 Runtime composition

共享 Agent Tool：`evolution_stable_promotion_runtime_admission_delivery` 新增：

- `queue <Admission ID>`；
- `inspect-dispatch <Admission ID>`；
- `run-worker`；
- `inspect-worker`。

Slash：

```text
/evolution stable-promotion-admission-delivery queue <Admission ID>
/evolution stable-promotion-admission-delivery inspect-dispatch <Admission ID>
/evolution stable-promotion-admission-delivery run-worker
/evolution stable-promotion-admission-delivery inspect-worker
```

New UI、CLI 和 Textual TUI 继续共享 Slash Router、Tool Registry 与 Engine。Moderate/Bypass 不新增二次确认；Lockdown 继续拒绝治理
写入。`RuntimeServiceOverrides.stable_promotion_runtime_admission_transport` 是当前显式注入点；未绑定 transport 不创建伪 Worker。

## 验收标准

- [x] exact 7b2a1 signed outbound 才能入队；
- [x] 并发 enqueue 幂等，different Submission 冲突关闭；
- [x] owner digest、claim epoch、attempt 和 lease 的 durable fencing；
- [x] expired claim 被新 owner 接管，旧 owner settlement 被拒绝；
- [x] retry 使用 durable exponential backoff 和有界 budget；
- [x] permanent/exhausted failure 进入不可自动重领的 dead-letter；
- [x] authenticated transport Receipt exact binding 后才 ACK；
- [x] Local adapter 真实执行 7b2a1 Control Plane receive/inspect；
- [x] event chain、主记录、outbound 与 Receipt 交叉验证，篡改失败关闭；
- [x] Engine start/stop/run/snapshot、Runtime override、Agent Tool 和 Slash 共用同一 Worker；
- [x] policy timeout/lease、backoff、scan、attempt、jitter 有界验证；
- [x] Window、长期指标、Promoted Outcome、learning、promotion、execution authority 不扩张；
- [x] 真实 Release/Stable Deployment/Harness/Admission/Ed25519 场景与定向小回归通过；
- [x] Ruff、compile、diff check；按用户要求未运行全量测试。

## 自我审视与下一步

本切片关闭了 durable automatic dispatch 的状态机缺口；后续 7b2a2b 已关闭 HTTPS/mTLS 网络边界。当前仍有这些缺口：

- dead-letter 暂无人工签名 requeue/abandon 和 retention；
- Control Plane server 尚未进入独立 daemon/discovery 与证书热重载；
- macOS 本机真实 fixture 不能替代 Linux/Windows 网络矩阵。

[EVO-05.7b2a2b](EVO-05-7b2a2b-authenticated-runtime-admission-http-transport.md) 已复用 bounded TLS HTTP common，交付
installation→Control Plane 固定 endpoint/media type、双向证书校验与 current/next pin、canonical Submission/Receipt Base64、严格
HTTP parser、并发/限流、真实 TLS loopback和 config 自动装配。
[EVO-05.7b3a](EVO-05-7b3a-stable-promotion-observation-chain-cursor.md) 又完成 installation 本地 durable chain cursor；
下一步是 7b3b signed revision delivery，不得从单个网络 Receipt 或本地 Cursor 直接跳到 promoted Outcome。
