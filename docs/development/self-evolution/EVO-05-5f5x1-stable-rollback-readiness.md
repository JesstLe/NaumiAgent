# EVO-05.5f5x1 Stable Rollback Readiness

## 目标

在签发 Stable Rollout Authorization 之前，只读证明某个已完成 Population 中的 exact Stable Deployment 当前仍可通过
ARC-07 active pointer 返回它的上一代二进制槽。该投影把 5f5w Completion、5f5m Stable Deployment Receipt、真实 active
pointer、prior activation、保留的 Release Slot 和原始 Boot Receipt 绑定为一个 content-addressed Readiness。

本切片只形成 binary rollback readiness，不签发 rollout authorization，不执行 pointer CAS，不触发 kill switch，也不授予
promotion authority。配置和数据迁移的回滚能力固定为 false，等待 ARC-07.6 的 snapshot/migration authority。

## 为什么不消费 Rollback Request

EVO-05.6a 的 Rollback Request 是运行 breach 发生后，由已暂停 kill switch 冻结的事故处置输入。健康 Stable rollout
尚未发生新的 breach，也不应预先制造事故 Request。把 6a Request 作为 rollout 前置会倒置因果关系，并强迫健康发布先进入 paused
状态。

正确顺序是：

```text
current 5f5w Completion
  + current Stable Deployment
  + retained prior slot / original Boot Receipt
  -> 5f5x1 binary rollback readiness
  -> 5f5x2 short-lived stable rollout authorization
  -> rollout / observation
  -> breach 时 6a Rollback Request
  -> 6b rollback executor
```

## 输入与动态重验

`EvolutionStableRollbackReadinessService.inspect()` 接收：

- `completion_receipt_id`：必须通过 5f5w `inspect()` 保持 current authority；
- `intent_id`：必须属于该 Completion 的 exact member，并通过 Stable Deployment `inspect()` 保持 active authority；
- Engine 组合的真实 `ReleaseSlotStore`。

Completion 与 Deployment 可并发读取；随后 Service 读取真实 active pointer、上一 generation 的 activation event，并通过
`resolve_booted_slot()` 重验上一槽的不可变 bytes 与原始 Boot Receipt。任何端口不可用、identity 漂移、pointer 竞争、slot/manifest
损坏或 Boot Receipt 不一致都会失败关闭。

## Exact lineage

Readiness 严格核对：

- Completion 的 workspace、Snapshot、member、plan、candidate version/target；
- Stable Intent 与 Deployment Receipt 的 member、Snapshot、plan、candidate slot；
- 当前 store pointer 等于 Deployment 已激活 pointer；
- 当前 generation 至少为 2，且 prior generation 恰好相差 1；
- `previous_pointer_sha256`、previous slot identity 与 prior activation 完全一致；
- prior activation 指向的 slot 仍保留，manifest 与 binary bytes 未变；
- 原始 Boot Receipt 仍能认证该 rollback slot，且 rollback slot 不等于当前 candidate slot。

这里不会重新启动上一槽，也不会修改 pointer。`resolve_booted_slot()` 只重验已安装不可变内容和原始启动回执，避免只读检查产生副作用。

## Readiness artifact

`EvolutionStableRollbackReadiness` 是 strict、frozen、schema v1 的确定性投影，冻结：

- 5f5w Completion Receipt、source-set 和 Population Snapshot identity；
- exact member、Stable Intent 与 Deployment Receipt identity；
- candidate version/target、slot 与 manifest identity；
- 作为未来 CAS 前置的当前 active pointer identity/generation；
- prior pointer、rollback slot、manifest、Boot Receipt 与 binary identity；
- binary/config-data/rollout/promotion 四种独立 authority。

`assessed_at` 取 Completion、当前 pointer activation 和 rollback Boot Receipt 三者时间的最大值，不读取 wall clock。相同 source
在不同调用中产生相同 Readiness ID/SHA；模型反序列化时重新计算摘要并校验 pointer generation 关系。

固定 authority：

```text
binary_rollback_readiness_authority = true
config_data_rollback_readiness_authority = false
stable_rollout_authority = false
promotion_authority = false
```

Readiness 不持久化为新的 durable receipt；它是针对 current sources 的只读、可重复计算快照。调用方不得缓存后当作执行授权。

## Agent Tool 与 Slash

- Agent Tool：`evolution_stable_rollback_readiness`；
- Slash：`/evolution stable-rollback-readiness <completion-receipt-id> <stable-intent-id>`；
- 两条通道调用同一 Service 和 `render_stable_rollback_readiness()`；
- Tool 是 read-only、non-destructive、并发安全，不需要确认；
- `lockdown` 禁止，其他 permission mode 可读；bypass 不改变 authority 判定。

## 验收标准

- 用真实 Release Slot Store 安装、boot 并依次激活 baseline/candidate 后得到 exact Readiness；
- 重复 inspect 的 ID/SHA 相同，strict JSON round-trip 成功；
- Tool 与 Slash 输出同一 renderer；
- actual pointer rollback 后旧 Deployment/readiness 检查失败关闭；
- 不属于 Completion 的 Intent 使用稳定错误码拒绝；
- Engine 默认组合 Completion、Deployment 与 Release Slot Store，并注册 Agent Tool；
- public lazy exports、Ruff、compile/import、命令索引、权限和相关小模块测试通过；
- 不运行全量测试。

## 自我审视与剩余边界

- Completion/Deployment 与 slot store 不共享数据库事务；本投影证明的是读取时的 content identity，不是未来执行权。5f5x2 必须在
  签发 authorization 时重新 inspect，并在执行器 CAS 前再次核对 exact pointer/readiness digest。
- binary readiness 不等于 config/data safety。5f5x2 必须显式判断 candidate 是否需要 migration/snapshot；需要时必须等待
  ARC-07.6 authority，不能把本切片的 false 升格为 true。
- 本地 Readiness 没有 exporter/signature envelope，不可作为跨控制面凭据。
- [EVO-05.5f5x2](EVO-05-5f5x2-stable-rollout-authorization.md) 已将 current Completion、Readiness、kill-switch
  generation、短期 expiry 和 single-use nonce 绑定为 member-scoped binary-only Authorization；executor 与 Promotion authority
  继续独立。
