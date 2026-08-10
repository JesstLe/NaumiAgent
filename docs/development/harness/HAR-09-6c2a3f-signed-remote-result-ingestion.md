# HAR-09.6c2a3f Signed Remote Result Ingestion

## 状态

已实现。

## 目标

把 `HAR-09.6c2a3e` 的短期 Remote Execution Authorization 收口为可恢复、可审计的真实 Runtime Eval 结果证据。
Worker 必须对完整、有界、typed 的逐 repetition 结果 manifest 做 Ed25519 签名；Control Plane 在 authorization
窗口内完成原子 durable admission，随后通过既有 Harness Store 写入 canonical H5a sample cohort 与 H5c Comparison。

本切片只完成一个远端 lane 的结果接收。它不等于完整 Behavioral Matrix，不授予 learning 或 promotion authority，也不把
Worker 自报、单个 response、过期前到达但未准入的数据视为权威结果。

## 协议对象

### Result Artifact

每个 repetition 固定包含：

- 连续且从 `0` 开始的 `sample_index`；
- 由 exact `ReleaseRuntimeEvalRequest` 机械生成的 `ReleaseRuntimeEvalProcessRequest`；
- content-addressed `ReleaseRuntimeEvalResponse` 与 exact remote platform identity；
- 重新计算的 canonical input/output byte 数；
- `exit_code=0` 与 `runtime_process_executed=true`。

Artifact 不接受日志、任意路径、环境变量、自由文本附件或模型生成的判定。单 response 受 512 KiB Runtime Eval 输出上限
约束，完整 manifest 受 authorization `max_result_bytes` 与最多 100 repetitions 双重约束。

### Signed Result Payload

Worker 签名 payload 固定绑定：

- exact Authorization ID/digest、Attempt ID、Run Grant digest 与 Runtime lease epoch；
- Delivery、Dispatch、Claim、Worker Identity/incarnation/epoch；
- Target Baseline Resolution、Outcome、Rollback Request 与原 H5c Comparison；
- release target、archive/manifest digest 与 Worker 安装后记录的 binary digest；
- suite id/digest、完整 Runtime Eval Request、repetitions；
- remote OS/architecture/Python/Naumi platform identity；
- 完整、连续、无重复的 process request/response cohort；
- started/completed time，以及 network deny、resource limit、process-tree containment 事实。

Target architecture 会机械校验主流别名：`x64 -> x86_64/amd64/x64`，`arm64 -> arm64/aarch64`。所有 response
必须使用同一个 exact platform identity，且 evaluated time 必须位于 signed start/completion window 内。

## 原子准入与过期恢复

首次提交严格分成两段：

```text
parse + bounded validation
  -> current authorization/Worker signature/Target Baseline lineage validation
  -> canonical Runtime receipt + H5 projection dry validation
  -> session SQLite BEGIN IMMEDIATE
  -> re-read exact Authorization + Worker Identity
  -> verify signature again
  -> verify admitted_at < authorization expiry
  -> persist immutable manifest + control-plane HMAC admission attestation
  -> write H5a cohort and H5c Comparison
  -> persist ingestion receipt
```

原子 admission 与 Authorization、Identity 共用 session SQLite，保证未获得 durable admission 的迟到自报失败关闭。Harness
Store 是独立权威库，因此不伪称跨库 ACID：manifest 一旦在 current window 准入，即成为恢复锚点；后续 H5 写入失败可在
authorization 过期后按 exact manifest 幂等恢复。相同 Attempt 的相同 manifest 收敛，不同 manifest 永久冲突。

Admission row 额外保存 control-plane HMAC attestation，绑定 manifest digest、authorization digest 与 admitted time。数据库
内容被修改、attestation 被替换或 key 不可用时读取失败关闭。key 仅在提交/读取 admission 时解析；默认 AgentEngine 构造
不会读取用户密钥。

## H5a/H5c 权威复用

远端 Worker 无权签发本地 Harness receipt。Control Plane 会：

1. 由 Worker process request/response、target manifest/binary digest 和 source-equivalent local slot identity 重建 typed
   `ReleaseRuntimeEvalReceipt`；
2. 调用 `EvolutionPostRollbackBehavioralLaneService.validate_remote_runtime_receipts()`，复用本地 lane 的 suite、原 baseline、
   repetitions、case/guardrail 与 active rollback authority 校验；
3. 调用 `record_remote_runtime_receipts()` 写入同一 `HarnessStore.record_eval_result()` H5a 路径；
4. 以原 H5c baseline cohort 与新 remote batch 构建并保存 canonical `HarnessEvalComparisonReceipt`；
5. ingestion receipt 固定保存全部 Runtime receipt digests、H5a result digests、H5c id/digest。

远端 Runtime receipt 保存在 Remote Result admission 中，不写入 `ReleaseSlotStore`，因为目标平台 binary 并不是控制平面本机
installed slot。这样避免把 Windows/Linux 远端执行伪装成本机 Release Slot authority。

## 动态检查与权威边界

`inspect` 每次重读 durable manifest、ingestion receipt、完整 H5a cohort 与 H5c receipt。任一 H5a 数量/digest、H5c
id/digest/current batch 漂移都会返回 `stale`，关闭 lane evaluation authority，但保留 admission 审计记录。

成功 receipt 只声明：

- `result_received=true`；
- `worker_signature_verified=true`；
- `durable_admission=true`；
- `h5a_ingested=true`、`h5c_recorded=true`；
- `lane_evaluation_recorded=true`。

它始终声明 `behavioral_matrix_recorded=false`、`learning_authority=false`、`promotion_authority=false`。完整跨平台矩阵由
`HAR-09.6c2b` 聚合，不允许单 lane 越权。

## 双通道入口

- Agent Tool：`evolution_post_rollback_remote_result`；
- CLI/TUI/New UI 共享 Slash：
  - `/evolution outcome-ingest-behavior submit '<manifest-json>'`；
  - `/evolution outcome-ingest-behavior inspect <manifest-id>`。

Tool 和 Slash 使用同一 Service。所有 Permission Mode 均不显示二次确认；bypass 直接通过权限策略，但不能跳过 Worker
signature、authorization window、Target Baseline lineage、bounded validation、admission attestation 或 H5 authority 校验。

## 验收证据

`tests/unit/test_post_rollback_remote_results.py` 与 `tests/unit/test_post_rollback_behavioral_lanes.py` 验证：

1. 完整 5-repetition signed cohort 在 authorization window 内原子准入并写入 H5a/H5c；
2. authorization 过期后的相同 manifest 重放从 durable admission 幂等恢复；
3. forged Worker signature 在 admission 与 H5 side effect 前失败；
4. 未准入的迟到结果失败，不能借“曾经授权”绕过 current window；
5. 缺 repetition、非连续 sample 或平台/architecture 不匹配在模型边界失败；
6. admission attestation 被篡改后读取失败关闭；
7. 两个独立 Service 并发摄入同一 manifest 收敛为一份 admission 与一份 receipt；
8. Behavioral Lane 的真实 installed-runtime fixture 通过 remote receipt API 写入 canonical H5a/H5c；
9. Tool、Slash、moderate 与 bypass 共享同一 Service 且不二次确认；
10. engine/tool registry、Ruff 与 Python compile 的窄范围回归通过。

## 当前不足与下一切片

本切片验证 Worker 签名的安装后 binary digest，并把它写入 content-addressed Runtime receipt；当前 release catalog 只公开
archive 与 manifest digest，尚未提供可由 Control Plane 独立比较的顶层 backend binary digest。后续 Release Catalog schema
演进应把 backend digest 提升为签名字段，从而把“Worker 记录”升级为“Control Plane 与 signed build manifest 双向比对”。

`HAR-09.6c2b1` 已实现 Behavioral Matrix Core：聚合本机 lane 与各 target 的 signed ingestion receipt，处理
missing/stale/conflict lane，并签发动态可撤权的总体 verdict。6c2b2 已完成 Workbench/New UI/TUI typed 详情；
长期指标完成前仍不得进入 learning 或 promotion。
