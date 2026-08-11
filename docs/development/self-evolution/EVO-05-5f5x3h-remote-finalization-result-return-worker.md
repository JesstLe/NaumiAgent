# EVO-05.5f5x3h Remote Finalization Result Return Worker

## 目标与边界

[EVO-05.5f5x3g](EVO-05-5f5x3g-authenticated-remote-installation-http-transport.md) 已把 signed Delivery Package 经
mTLS 送达安装端并形成 installation-signed ACK，但 ACK 明确不执行 writer。此前 ACK 后仍需人工
`execute-delivery-local -> ingest-delivery`，进程在 writer、Result 签名或回传期间退出时也没有统一恢复 Worker。

本切片交付安装端的 durable 双阶段 outbox：从 target journal 发现已 ACK 的 exact Delivery；在原 Execution Grant authority 下执行
expected-pointer writer，或只补签已落盘 writer fact；持久化 installation-signed Result；通过窄 Result transport 回传 Control Plane；
验证并落盘 exact Finalization Receipt，同时使 Control Plane Delivery 进入 `completed`。

本切片提供真实进程内 Control Plane adapter 和 Runtime transport 注入点，不虚构跨机器 Result 网络已经完成。生产 mTLS Result endpoint、
安装端独立 daemon、证书热重载与 Population Receipt aggregation 仍属后续切片。

## 为什么 outbox 从 execute 阶段开始

若只在 `result_signed` 后创建回传 outbox，writer 已提交但 installation key 暂不可用时仍没有 durable retry authority。因此 outbox 在
target journal 的 `received` 状态即创建，事件同时记录 `phase`：

```text
execute/queued
  -> execute/in_flight(owner digest, epoch, attempt, lease)
     -> return/queued(exact submission digest)
     -> execute/queued(retry backoff)
     -> execute/dead_letter

return/queued
  -> return/in_flight(owner digest, epoch, attempt, lease)
     -> return/completed(exact receipt digest)
     -> return/queued(retry backoff)
     -> return/dead_letter
```

每个 claim 都递增 `claim_epoch` 与 `attempt_count`。仅持有未过期 exact owner digest、epoch 和 lease 的 Worker 可以推进、重试、完成或
dead-letter；owner 原文不写入 SQLite。事件使用 content-addressed ID/SHA 和 previous digest 形成 append-only chain，主记录必须与链尾
完全一致。

## Journal 完整性与无饥饿同步

`EvolutionStableRemoteFinalizationTargetJournal.get/list_entries` 公开严格只读投影，并在反序列化前校验 ACK、writer、Result 原始 JSON
的 SHA-256。状态与 artifact 关系、ACK/package、writer authority、submission/package/member 也必须机械一致；篡改直接失败关闭。

Worker 不扫描固定的“最早 N 条 journal”再在内存过滤。那会让已完成旧记录永久占据窗口，使新 Delivery 饥饿。Store 在同一 SQLite 对
journal 与 result outbox 做 anti-join，只读取“journal 已有、outbox 尚无”的有界前缀，再逐条执行严格 restore。同步重复执行幂等，已完成
记录不会阻塞新记录。

## Writer 与崩溃恢复

execute phase 复用 x3d/x3e 的唯一 writer：

- grant 未过期且 journal 为 `received` 时，重验 Trust Policy、Credential、installation key、Release Store 与 expected pointer，
  再执行 CAS；
- writer 成功后 keyring/签名失败，journal 先持久化 `writer_committed`；
- 重试调用 `recover_stable_remote_finalization_submission()`，只读取并补签既有 durable finalization，禁止再次执行 writer；
- grant 已过期时同样只能 recovery；不存在原窗口内 writer fact 时 permanent dead-letter；
- journal 已为 `result_signed` 时返回 exact same Submission，不重复 writer 或签名。

writer 在当前 task 内同步完成，避免取消后遗留无人观察的后台写入。它受 Release Store 自身有界事务与校验约束，但 Python 无法安全中断
一个已进入本地 SQLite writer 的同步调用；专用安装 daemon 的进程级 watchdog 属于后续 Harness 运维切片。

## Result transport 与 Receipt 绑定

`EvolutionStableRemoteFinalizationResultTransport.submit()` 只接受 exact Delivery Package、Submission 和显式 `late_recovery`，返回 typed
`EvolutionStableRemoteFinalizationReceipt`。Worker 在回传时重新计算当前时间：即使 Result 在 grant 窗口内签署，只要回传发生在 expiry
后，也必须走 x3e 的受限 late-result ingest。

`LocalStableRemoteFinalizationControlPlaneTransport` 调用共享 `EvolutionStableRemoteFinalizationDeliveryService.ingest_result()`：
Control Plane 继续重验 Authorization、Grant、Credential、installation signature、Result binding 与 single-use durable facts；随后
Delivery 必须已 ACK 才能进入 `completed`。返回 outbox 只有在 Receipt 的 execution package 与 exact submission 完全相同后才完成。
ACK 尚未可见、临时 I/O 和 timeout 可重试；artifact/chain/Receipt 冲突为 permanent failure。

该 adapter 是可信进程边界，不是生产网络认证证据。`RuntimeServiceOverrides.stable_remote_finalization_result_transport` 是唯一注入点；
未绑定时 Engine 不创建伪 Worker，手动 run/inspect 明确报告缺少 authenticated Result transport。

## 周期、退避与生命周期

配置位于 `harness.stable_remote_finalization_result_return`：`enabled`、`interval_seconds`、`max_empty_backoff_seconds`、
`max_failure_backoff_seconds`、`journal_scan_limit`、`return_scan_limit`、`claim_lease_seconds`、`result_timeout_seconds`、
`retry_base_seconds`、`retry_max_seconds`、`max_attempts`、`shutdown_drain_seconds`、`jitter_ratio`。

Pydantic 与 Worker policy 双层拒绝 NaN、反向 backoff、越界 scan/attempt、无效 lease，以及不小于 lease 的 Result timeout。周期 Worker
启动前先跑一次有界 recovery pass；本地 receive/execute 会 wake Worker；shutdown 等待当前 Result transport 在 drain deadline 内收口，
超时才取消并记录 forced shutdown。execute 与 return 共享总 attempt budget，避免毒性 Delivery 在阶段切换后重置无限重试。

## 双通道

Agent Tool、CLI、TUI 与 New UI 共享 `EvolutionStableRemoteFinalizationTool` 和同一个 Engine Worker：

- `/evolution stable-remote-finalization run-result-return-worker`
- `/evolution stable-remote-finalization inspect-result-return-worker`

Tool action 使用相同名称。没有新增权限确认；现有 Evolution Permission Rule 保持有效，bypass 继续直接通过。Renderer 显示同步、claim、
writer/result、Receipt、retry、dead-letter、failure、forced shutdown 与下一延迟计数。

## 验收证据

- [x] 真实 Release Slot、target journal、installation key 与 Control Plane Service 完成 ACK→writer→signed Result→Receipt；
- [x] target outbox 与 Control Plane Delivery 最终都为 `completed`，重复 pass 无副作用；
- [x] writer 后 keyring 暂不可用会 durable retry，grant 过期后只补签既有 writer fact 并走 late ingest；
- [x] transport 临时失败持久化 backoff，下一次返回 exact Receipt；
- [x] owner/epoch/lease takeover 会 fence 陈旧 Worker；
- [x] journal artifact digest 篡改失败关闭；
- [x] shutdown drain 等待 in-flight Result transport，不产生 forced cancellation；
- [x] Runtime composition 可注入 Result transport 并负责 startup/shutdown；
- [x] Agent Tool 与 Slash 共享 run/inspect 后端；
- [x] 相关 x3e/x3f、Runtime services、Ruff、compile、public lazy exports 与文档治理通过；
- [x] 按用户要求未运行全量测试。

## 自我审视与下一步

本切片完成了进程内真实 Result durable recovery 闭环，但尚不能宣称 fleet 网络闭环生产化：

- Result transport 只有 Protocol 与可信本机 adapter，跨机器认证 endpoint 尚未实现；
- synchronous Release Store writer 没有进程级 watchdog，安装 daemon crash supervision 尚未接入；
- dead-letter 仍缺签名人工审查、requeue/abandon 和 retention；
- macOS/Linux/Windows 真实 daemon 与网络故障矩阵尚未形成发布证据；
- 单 member Receipt 尚未聚合为 Population Stable Rollout Completion Authority。

[EVO-05.5f5x3i Authenticated Result Return HTTP Transport](EVO-05-5f5x3i-authenticated-result-return-http-transport.md) 已复用 x3g
的独立 mTLS 身份、同连接 pin、限长、timeout、并发与安全错误边界，并增加 installation leaf→member 的精确授权。下一最小切片转为
[HAR-10.9a installation daemon supervision/service discovery](../harness/HAR-10-9a-remote-finalization-installation-daemon-supervision.md)
已完成；下一步才进入 EVO-05.5f5x3j Population Receipt aggregation，不能把单 member Result/daemon 健康
冒充 Population completion。
