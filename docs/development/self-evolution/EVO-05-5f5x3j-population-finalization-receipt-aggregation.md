# EVO-05.5f5x3j Population Finalization Receipt Aggregation Authority

## 1. 目标与依赖裁决

x3d–x3i 与 HAR-10.9a 已经完成逐 installation member 的真实 Release Store CAS、installation-signed Result、
Control Plane Receipt、双向 mTLS 和 daemon 监督。但单 member Receipt、daemon 健康或 Result Worker 计数都不能证明
exact signed Population 已全部完成 stable binary finalization。

本切片消费：

- current `ReleasePopulationSnapshot` 的 exact member/credential 集合；
- x3d/x3h/x3i 已写入 Control Plane Evolution DB 的逐 member
  `EvolutionStableRemoteFinalizationReceipt`；
- original Authorization、Consumption、Execution Grant、Rollout Control 与 Trust Policy；
- current Population Credential 和 installation signature。

输出 durable `EvolutionStableRemotePopulationFinalizationReceipt` 与动态 View。它证明 binary-only Population
finalization 完整，不执行新的 Release Store 写入，不授予配置/数据 finalization、promotion、rollback 或 deployment 权限。

## 2. 为什么不能直接复用 5f5w

5f5w `EvolutionStablePopulationCompletionReceipt` 是 stable rollout 之前的阶段完成权威，来源是 5f5r stage-completion
evidence。x3j 是 rollout 执行之后的 fleet finalization 权威，来源是远端安装端真实 writer fact 与独立安装签名。

两个 Receipt 的时间、source graph 和权限完全不同：

```text
5f5w: all members passed stable-stage evidence
  -> x1 rollback readiness
  -> x2 single-use rollout authorization
  -> x3 per-member local/remote finalization
  -> x3j all exact remote member receipts aggregated
```

因此 x3j 新建独立 artifact/store/service，不改写 5f5w，也不把部署前 Completion ID 冒充部署后 finalization。

## 3. 短期 capability 与长期完成事实

Authorization、Readiness Claim 和 Probe 都是短期执行 capability。member 已在窗口内完成真实 writer、签名并由 Control
Plane 接收后，capability 到期不能自动删除完成事实，否则 Population authority 最多存活 60–300 秒。

`inspect_aggregation_material()` 为 x3j 提供专用 post-execution 重验：

- exact durable Package 与 Consumption 仍匹配 Receipt；
- Authorization source row、current Rollout Control generation 和 current Trust Policy 仍匹配；
- Execution Grant 的独立 Rollout Control signature 仍可由 current policy 验证；
- current Population Credential 与 Authorization/Result 的 member、credential、公钥一致；
- installation Result signature 和 Result→Grant→writer pointer binding 有效；
- 不恢复、延长或重新使用已经消费/过期的 bearer capability；
- `remote_active_pointer_current_unverified=true` 继续诚实保留，x3j 不伪造远端 fresh pointer probe。

这样历史 member completion fact 可在 capability 到期后继续认证，同时 signer 撤销、control pause、credential/Snapshot
替换、durable source 篡改仍会动态撤权。

## 4. exact Population member 集合

Service 只接受 exact current stable Population Snapshot。Control Plane 按 Snapshot ID 从 member Receipt 表读取最多
10001 条记录，10001 立即失败，正常上限为 10000。

逐 member 必须满足：

1. Snapshot 中每个 member 恰好一个 Receipt；
2. 不允许缺员、额外 member 或同一 member 多 Receipt；
3. Receipt 中 Snapshot ID/SHA 与 current Snapshot 完全一致；
4. credential ID/SHA/public-key SHA 与 Snapshot 的 exact credential 一致；
5. 所有 member 绑定同一 5f5w Completion ID/SHA、candidate version 与 Rollout Control generation；
6. member 按 ID 稳定排序，不采用“最新时间获胜”等猜测策略解决冲突。

重复同一 Grant/Submission 已由 x3d/x3i 幂等收敛为同一 Receipt。若同一 member 后续真的产生第二个不同 Receipt，x3j
将其视为治理冲突而不是静默选择；既有 Population Receipt 立即撤权，writer 也拒绝签发新成功外观。

## 5. durable artifact

每个 `EvolutionStableRemotePopulationFinalizationMember` 内容寻址并冻结：

- exact member、credential 与 installation public key；
- member Receipt、Authorization、attempt、Grant、Result identity；
- target `ReleaseStableMemberFinalization` identity；
- expected pointer ID/SHA/generation；
- target completed time 与 Control Plane recorded time。

Population Receipt 再冻结：

- workspace hash；
- Population Snapshot ID/SHA/sequence/denominator；
- original 5f5w Completion ID/SHA 与 candidate version；
- common Rollout Control generation/event；
- 全部稳定排序 member sources；
- source-set SHA 与 deterministic `finalized_at`（所有 member `recorded_at` 的最大值）；
- binary-only historical fact 为 true；配置/数据与 promotion authority 永远为 false。

Receipt ID/SHA 由 canonical JSON 计算。它只记录 `population_snapshot_prevalidated=true`，不宣称跨数据库的 Snapshot
检查与 Evolution SQLite writer 处于同一原子事务；current authority 只由动态 View 表达。

## 6. SQLite writer fence

`EvolutionStableRemotePopulationFinalizationStore.record()` 在 Evolution DB 的 `BEGIN IMMEDIATE` 中：

1. 重新查询 exact Snapshot ID 的全部 member Receipt，限制 10001 条；
2. strict 解析并重建稳定排序 member source，拒绝重复 member；
3. 与待写 Population Receipt 的完整 member tuple 精确比较；
4. 从 exact member Authorization 机械重算 workspace、Snapshot、Completion、candidate 与 Control 元数据，拒绝调用者伪造
   top-level 字段；
5. 对每个 member 重读并 strict 比较 durable Package、Authorization、Consumption；
6. 重读 workspace 的 latest Rollout Control event，要求 generation/ID/SHA/state 完全一致，损坏 JSON 使用稳定错误码失败关闭；
7. 以 unique source-set SHA 幂等写入。

因此 service inspect 后到 writer 前新增、替换或损坏 member Receipt，或 rollout control 变化，都会在同一 writer transaction
中失败关闭。Population Snapshot/Trust Policy 位于外部 authority store，无法参与此 SQLite 事务；Service 在 writer 前后动态
重验，若期间漂移，历史 Receipt 可存在但 View 不具备 current authority。

## 7. 动态 View 与撤权

`inspect()` 每次重验：

- Population Receipt durable JSON 与 content identity；
- 它仍是该 Snapshot 最新的 Population finalization Receipt；
- Population Snapshot source/latest/trust/validity 当前有效且 identity/denominator 一致；
- 当前 member Receipt 集合与 durable member tuple 完全相同；
- 所有 member aggregation material 仍通过当前 Control、Trust、Credential、双签名与 durable source 重验。

任一条件失败，`stable_population_finalization_authority=false`，并返回稳定、去重、有界的 reason：Receipt source changed、
newer finalization、Snapshot stale、member set changed 或 member authority changed。历史
`stable_population_finalization_fact` 不被重写，便于审计。

## 8. Agent Tool、Slash 与 UI 同源

- Agent Tool：`evolution_stable_remote_population_finalization`；
- 签发：`/evolution stable-remote-population-finalization complete [population-snapshot-id]`；
- 重验：`/evolution stable-remote-population-finalization inspect <population-finalization-receipt-id>`；
- Tool 与 Slash 共用同一 Service 和 renderer，New UI/Textual TUI 继续消费共享 Tool Result；
- permissive/moderate/strict/bypass 均允许且不二次确认，lockdown 拒绝；
- 该操作只新增 content-addressed 聚合证据，不执行 target writer，因此标记 non-destructive、concurrency-safe。

## 9. 验收证据

- [x] 两个真实 Population member 分别完成 Claim→Probe→Authorization→Grant→Release Store CAS→installation
  signature→Control Plane Receipt；
- [x] 八路、四个 Service 并发 complete 收敛为一个 Population Receipt 和一行 SQLite；
- [x] member capability 到期后，已完成事实仍可认证且不会复活 bearer authority；
- [x] 缺少任一 member Receipt 时拒绝完成；
- [x] 同一 member 第二个不同 Receipt 会撤销旧 View，service 与 writer fence 均拒绝冲突；
- [x] forged top-level candidate/source metadata 即使重新计算 content identity，也会被 writer 的 Authorization 重算拒绝；
- [x] member Receipt JSON 篡改后历史 Population fact 保留但 current authority 撤销；
- [x] Rollout Control pause 或新 Population Snapshot 立即动态撤权，损坏 Control event 以稳定领域错误失败关闭；
- [x] strict JSON round-trip、16 MiB artifact 上限与 10000 member 读取上限；
- [x] Engine 默认组合 Store/Service，public lazy exports、Agent Tool、Slash 和权限同源；
- [x] 相关 x3d/x3h/x3i/HAR-10.9a/Engine/Tool/Permission 小模块测试、Ruff、compile 与文档治理通过；
- [x] 按用户要求未运行全量测试。

## 10. 自我审视与后续边界

本切片完成的是 Control Plane 内部的 durable Population finalization authority，不是公开透明日志或 promotion：

- Population Receipt 没有 exporter/signature envelope，不能作为跨控制面的 bearer artifact；
- Snapshot 与 Evolution evidence 位于两个数据库，动态 View 可发现竞态并撤权，但没有分布式原子快照；
- 同 member 多 Receipt 当前 fail closed，尚无显式 supersession/仲裁 workflow；
- `remote_active_pointer_current_unverified` 仍存在，长期 fleet drift 需要周期 fresh Probe/attestation；
- Result dead-letter review/requeue/abandon/retention、证书热重载和 OS service 安装仍是独立运维切片；
- config/data finalization 等待 ARC-07.6；promotion 继续走独立审批、签名与 package authority。

下一最小切片不再扩写 x3 transport。应跨文档选择能够消费 x3j current View 的下一个用户可交付闭环，优先补
Population finalization 在 Workbench/Completion Receipt UI 的结构化可视化与失效原因，而不是继续堆叠执行权限。
