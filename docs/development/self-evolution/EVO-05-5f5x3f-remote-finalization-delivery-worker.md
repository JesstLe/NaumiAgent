# EVO-05.5f5x3f Remote Finalization Delivery Worker

## 目标与边界

x3e 已提供 durable outbox、owner/epoch/lease fencing、installation-signed ACK 与目标 journal，但投递仍需人工
`claim-delivery -> receive-delivery-local -> ack-delivery`。本切片把这段路径收口为可由 Runtime composition 注入的认证安装传输端口和
有界周期 Worker，并补齐 ACK timeout、durable retry budget、dead-letter 与 shutdown drain。

本切片只自动化“package 到达目标并形成 ACK”。ACK 仍固定声明 `writer_executed=false`，Worker 不调用 Release Store writer，
也不生成 Finalization Receipt。目标执行、Result 回传和 Control Plane ingest 继续由 x3d/x3e 的独立授权通道完成。

仓库当前没有生产 HTTP/mTLS 安装端客户端，因此本切片没有虚构网络已上线：它交付严格的 async transport Protocol、真实的本机安装端
适配器，以及 Runtime composition 注入点。跨主机 HTTP/mTLS adapter、endpoint discovery、证书轮换和网络级集成测试属于后续切片。

## 状态机扩展

```text
queued
  -> in_flight(owner digest, epoch, attempt, lease)
     -> acknowledged(exact installation ACK)
     -> queued(retry backoff)
     -> dead_letter(exhausted or permanent failure)
```

`dead_letter` 是 append-only event chain 的权威终态之一：

- 只能由仍有效的 owner/epoch/lease claim 写入；
- 必须绑定规范化 failure code；
- 不在 claim 扫描集合中，跨任意未来时间都不会被自动重领；
- attempt/epoch 不得在 dead-letter transition 中漂移；
- 当前切片不提供人工 requeue，避免未经审查让 poison package 重新进入自动投递。

## Authenticated Installation Transport

`EvolutionStableRemoteFinalizationInstallationTransport` 只有一个窄方法：

```python
async def receive(package) -> EvolutionStableRemoteFinalizationDeliveryAck
```

返回值必须是 x3e 定义的 installation-signed ACK。Control Plane 仍通过
`EvolutionStableRemoteFinalizationDeliveryService.acknowledge()` 查找 current Credential 并完成签名、member、grant、delivery、时间和
live claim 验证；传输实现不能绕过 authority。

`LocalStableRemoteFinalizationInstallationTransport` 是真实可执行的本机 adapter：

1. 先核对 package member 与本机 Credential；
2. 使用 installer-owned Trust Policy 重验双层 control signature；
3. 调用目标 `EvolutionStableRemoteFinalizationTargetJournal.receive()`；
4. 事务写入 journal 后才由 installation key 签 ACK；
5. member mismatch 属于 permanent failure，直接 dead-letter。

本机 adapter 不在取消后遗留后台线程写入；journal transaction 在 Worker task 内完成。未来网络 adapter 必须实现同一 Protocol，不能降低
ACK 的密码学与 durable journal 语义。

## Worker 策略

`EvolutionStableRemoteFinalizationDeliveryWorkerPolicy` 统一约束：

- 周期与空轮/失败指数退避上限；
- 每轮最大 `scan_limit`；
- claim lease；
- 必须小于 claim lease 的 ACK timeout；
- retry base/max 和 durable `max_attempts`；
- shutdown drain deadline；
- 有界 jitter。

每轮行为：

1. 从 Store claim 一个到期 Delivery；
2. 在 ACK timeout 内调用认证传输；
3. 通过共享 Service 验证并持久化 ACK；
4. timeout、I/O 和显式 retryable transport failure 进入 durable backoff；
5. permanent failure 或 attempt 达到预算时进入 dead-letter；
6. claim/settlement 异常形成明确 failure code，不把失败计为成功；
7. 达到 scan limit 或没有到期记录时结束本轮。

Worker 的 Snapshot 暴露 pass、claim、ACK、retry、dead-letter、failure、forced shutdown 和下一延迟计数。owner 原文仍只存在内存，SQLite
只保存 SHA-256。

## Lifecycle 与双通道

`RuntimeServiceOverrides.stable_remote_finalization_transport` 是唯一 composition 注入点。未绑定 transport 时：

- Engine 不创建伪 Worker；
- 手动 `run-delivery-worker`/`inspect-delivery-worker` 明确报告“未绑定认证安装传输”；
- 原有手工 queue/claim/ACK 路径继续可用。

绑定 transport 且配置启用时，Engine startup 先执行一次有界 recovery pass，再启动周期循环；shutdown 设置 stop/wake，等待当前 ACK
在 drain deadline 内收口，超时才取消并记录 `forced_shutdown_count`。新 queue 会主动 wake 已运行 Worker。

Agent Tool 与 CLI/TUI/New UI 的 `/evolution` 共享同一个 Engine Worker：

- `run-delivery-worker`
- `inspect-delivery-worker`

没有新增权限确认；lockdown 继续沿用 `evolution_stable_remote_finalization` 的 Permission Rule，bypass 保持直通。

## 配置

配置位于 `harness.stable_remote_finalization_delivery`，包括：

- `enabled`
- `interval_seconds`
- `max_empty_backoff_seconds`
- `max_failure_backoff_seconds`
- `claim_lease_seconds`
- `scan_limit`
- `ack_timeout_seconds`
- `retry_base_seconds`
- `retry_max_seconds`
- `max_attempts`
- `shutdown_drain_seconds`
- `jitter_ratio`

Pydantic 与 Worker policy 双层验证 timeout/lease、retry、backoff、scan 和数值有限性；配置不能通过 NaN、负数或反向边界。

## 验收证据

- [x] 真实 x3e package 通过本机 Trust Policy、target journal 与 installation key 自动形成 ACK；
- [x] ACK 被共享 Service 重新验签，Delivery 进入 `acknowledged`，且 `writer_executed=false`；
- [x] ACK timeout 被取消并持久化为 retry failure code；
- [x] retryable transport failure 先 backoff，达到 max attempts 后进入 dead-letter；
- [x] dead-letter 跨未来时间不可再次 claim；
- [x] shutdown 在当前 ACK 完成后收口，无 forced cancellation；
- [x] Runtime composition 可注入 transport，Engine 能创建、运行并停止 Worker；
- [x] Agent Tool 与 `/evolution stable-remote-finalization run-delivery-worker` 共享同一 Worker；
- [x] 相关 x3e 回归、Runtime port bundle、Ruff、compile 和 public API smoke 通过；
- [x] 未运行全量测试。

## 自我审视与下一步

本切片关闭了自动 claim/ACK、重试预算和 shutdown lifecycle 缺口，但不等于跨机器网络已交付：

- 生产 HTTP/mTLS adapter、安装端 daemon endpoint、证书 pinning/轮换、DNS/service discovery 尚未实现；
- dead-letter 尚无签名审查、人工 requeue/abandon 和 retention；
- ACK 后 target writer/result 的主动回传仍由独立通道触发；
- 没有 macOS/Linux/Windows 三端真实网络故障矩阵；
- Snapshot 已进入 Tool/Slash，但尚未做专用 Workbench 队列页面。

下一最小切片应优先实现 **EVO-05.5f5x3g Authenticated Remote Installation HTTP Transport**：复用本 Protocol，交付双向认证、
endpoint 身份绑定、请求大小/超时、幂等 ACK、证书轮换与三平台 loopback 集成验证。只有远端网络 transport 和 Result 回传闭环完成后，
才进入 Population member Receipt aggregation。
