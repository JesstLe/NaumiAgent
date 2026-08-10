# HAR-09.6c2a3b Remote Dispatch 与原子容量预留

## 状态

已实现。

## 目标

把一个 current Target Baseline Resolution 转换为可审计的 `queued` 远端评测任务。该转换必须同时消费：

1. exact Remote Lane Placement；
2. current signed Release Channel target baseline；
3. exact active Worker incarnation；
4. Registry 中最新 durable Health Report；
5. Worker contract 的平台、隔离、资源和协议能力；
6. Registry 的原子 capacity reservation；
7. 原 Before/After Evidence 的 exact lane 与仓库内受信 Eval suite。

任何一项缺失、变化或过期都不得建立 Dispatch authority。

## 权威链

`EvolutionPostRollbackRemoteDispatchService` 依次验证：

```text
Rollback Request
  -> Coverage Contract
  -> Before/After Evidence exact comparison lane
  -> Remote Lane Placement
  -> Target Baseline Resolution + current Channel/Builder trust
  -> exact active Worker contract
  -> latest durable Health Report
  -> atomic capacity reservation
  -> immutable queued Remote Dispatch
```

Coverage Contract 负责绑定 Before/After Evidence ID/SHA；Dispatch 不读取测试夹具或内存 view 作为 lineage。
suite 必须重新从当前 workspace 的 `docs/harness/evals/<suite-id>.yaml` 加载并校验 fixture digest，禁止硬编码
suite 名称或执行预算。

## 预算与 reservation TTL

- `case_execution_budget_ms` 是 suite 全部 case 的 `max_duration_ms` 之和；
- `total_execution_budget_ms = case_execution_budget_ms * original_before_samples`；
- repetitions 必须与原 lane 的 before/after samples 相等，且范围为 5..100；
- execution seconds 向上取整；
- overhead 为 `max(10s, ceil(execution_seconds * 20%))`；
- reservation TTL 为 execution seconds 与 overhead 之和，并且必须落在 5s..24h；
- TTL 还必须满足 exact Worker contract 的 `max_wall_seconds`。

当前 `protocol-hello-core` 的 6 个 case 每个 100ms、原 lane 5 次重复，因此总执行预算为 3000ms，
reservation TTL 为 13s。

## 并发与失败语义

- Registry 在 `BEGIN IMMEDIATE` 事务中对 exact worker/instance/epoch 原子计数并插入 reservation；
- 同一 Target Baseline 的 Dispatch identity、job identity 与 reservation identity 均由完整事实摘要决定；
- 进程内同 key 锁减少重复工作，SQLite unique constraint 负责跨进程幂等；
- snapshot 后发生容量竞争时，以原子 reserve 结果为准，返回稳定的 capacity exhausted code；
- reserve 后、Dispatch durable write 前发生已知错误或取消时，使用原 queued timestamp 释放 reservation；
- compensation 自身失败会显式返回 `post_rollback_remote_dispatch_cleanup_failed`，不得吞错；
- durable Dispatch、health、baseline、registration、suite 或 reservation 任一失效，动态 inspect 会撤销
  `dispatch_authority`。

## 明确未授予

Remote Dispatch 只是持久化的 `queued` job authority：

- `worker_claimed=false`；
- `attempt=1`，本切片不伪造 retry authority；
- `transport_delivered=false`；
- `execution_authority=false`；
- `result_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

它不下载或安装 target baseline，不向 Worker 传输 payload，不签发 challenge/lease，不运行测试，也不接受结果。

## 双通道入口

- Agent Tool：`evolution_post_rollback_remote_dispatch`；
- CLI/TUI/New UI 共享 Slash：
  `/evolution outcome-dispatch-behavior <rollback-request-id> <comparison-id> <channel>`。

两者共用同一 Service。所有正常模式都不要求二次确认；bypass 仍跳过确认，但不会绕过 lineage、health、
capacity、trust 或 durable authority。

## 验收证据

`tests/unit/test_post_rollback_remote_lane_placements.py` 覆盖：

1. real workspace suite YAML 与 fixtures 加载；
2. exact baseline/placement/Worker/Health lineage；
3. 预算与 TTL 的机械推导；
4. 两个独立 Service 实例并发 queue 仍收敛到同一 Dispatch/reservation；
5. capacity reservation 持久化；
6. Agent Tool、Slash、moderate/bypass 无二次确认；
7. newer draining Health Report 动态撤权；
8. capacity exhaustion 不产生 Dispatch artifact；
9. Before/After Evidence digest 篡改 fail closed 且不占用 capacity；
10. durable write failure 自动释放或终结 reservation。

## 下一切片

`HAR-09.6c2a3c` 必须在本 Dispatch 上实现 authenticated Worker claim 与 fenced lease。claim 之前仍不能传输
baseline 或签发执行授权；claim/lease 完成后，再由独立切片实现 retry attempt authority、execution
authorization、签名结果摄取和矩阵聚合。

`ARC-04.1d` 已先交付 supervisor-attested Ed25519 Worker Identity，避免 6c2a3c 仅凭公开 worker/instance/epoch
字段接受冒名 claim。
