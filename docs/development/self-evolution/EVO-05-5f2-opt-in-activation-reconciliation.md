# EVO-05.5f2 Opt-in Activation and Reconciliation

## 目标

消费 current [EVO-05.5f1](EVO-05-5f1-opt-in-deployment-intent.md) Deployment Intent，使用 ARC-07
expected-pointer CAS 把当前本机安装原子切换到已安装、已 boot、已验签的 candidate slot，并形成可从
pointer history 机械恢复的 append-only Deployment Receipt。

本切片只部署当前 local installation。它不启动新进程、不实现全局 1% population assignment、不开放
percentage/stable rollout，也不执行 Git、发布、回滚或 promotion。

## ARC 最小前置

为了让 crash reconciliation 不依赖当前 tail，ARC-07.5a 增加两个只读/证明能力：

1. `ReleaseSlotStore.get_activation_event(generation)` 在完整重放 `1..N` hash chain、验证 singleton tail 后，
   精确读取任一 immutable historical generation；
2. v2 `ReleaseActivePointer` 把 `activation_authority.kind/id/sha256` 纳入 pointer digest。5f2 使用
   `evolution_opt_in_deployment_intent + exact intent ID/digest`；旧 v1 pointer 不序列化空字段，摘要兼容。

仅匹配 generation、slot 和时间不足以证明因果。未绑定 Intent 的外部调用即使切到了相同 candidate slot，
也不能被 5f2 reconcile 认领为本次部署。

## 执行链

`EvolutionRevalidationOptInDeploymentService.deploy()`：

1. 幂等读取既有 Receipt；
2. 重读 exact durable Intent，并先检查预期 generation 是否已存在；
3. 若已存在，只有 exact authority/previous pointer/slot/boot 全部匹配才机械补写或复用 Receipt；
4. 若尚未切换，最后一次重验 5f1 的 interaction、control、trust、slot、boot、pointer 与 expiry；
5. 要求 current pointer 仍等于 Intent 的 exact previous pointer；
6. 调用 ARC-07 `activate()`，同时传入 expected pointer digest 与 content-addressed Intent authority；
7. 验证返回的 generation、previous link、candidate slot、Boot Receipt、old-slot retention 与 authority binding；
8. SQLite `BEGIN IMMEDIATE` 内重读 exact stored Intent，幂等写入 Deployment Receipt；
9. 返回动态 Deployment View。

两个不同 Service 实例并发时，第二个可能在首次观察 generation missing 后失去 CAS。所有失权、pointer conflict
与 CAS-conflict 出口都会再次重读 authority-bound generation；若是同一 Intent 的结果则收敛到同一 Receipt，
若 generation 被其他 authority 占用则失败关闭。

## 崩溃对账

`reconcile()` 与 `deploy()` 共用同一机械状态机：

- pointer 未切换且 Intent 仍 current：安全执行一次 CAS；
- exact authority-bound pointer 已切换但 Receipt 缺失：从 immutable event 重建同一 content-addressed Receipt；
- pointer 切换后又发生 rollback/后续 generation：仍可读取原 deployment generation，记录历史事实，但
  `active_deployment_authority=false`；
- 预期 generation 指向未绑定或不同 authority/slot：`generation_conflict`，不写 Receipt、不重复切换；
- activation time 不在 Intent authority window：拒绝生成 Receipt；
- activation chain 缺口、singleton/tail 不一致或 artifact 损坏：失败关闭。

Receipt 不存“正常路径/恢复路径”标记，因为该标记会让同一 pointer 事实产生两个不同 digest。Receipt 只由 exact
Intent 与 exact activation event 派生，因此正常落盘和 crash reconcile 得到相同 identity。

## 两类 authority

`deployment_fact_authority=true` 表示 exact source Intent 仍可重读，且 ARC-07 完整 hash chain 中存在 exact
authority-bound activation fact。
即使之后 rollback，它仍是历史事实。

`active_deployment_authority=true` 还要求：

- receipt pointer 仍是 active tail；
- candidate immutable slot 与 Boot Receipt 仍 current；
- current Build Trust Policy 仍信任 exact builder key 和 detached Attestation；
- authoritative Harness opt-in enrollment 仍与 Intent 相等。
- rollout control 仍是 Intent 绑定的 exact active generation。

因此 control pause/generation 推进、key 撤销、policy 轮换、slot/boot 损坏或后续 pointer 推进不会抹除历史事实，
但会动态撤销“当前可信部署”状态。

## 验收结果

- 两个独立 Service、八个并发 deploy 调用只形成一个 authority-bound generation 和一个 Receipt；
- old slot 保留，active pointer 精确切到 candidate，process 未启动；
- pointer 切换后注入 Receipt write failure，再 rollback 到旧版本，reconcile 仍从历史 generation 补写 Receipt；
- 补写后的 deployment fact 有效，但 active deployment 已失权；
- 未绑定 Intent 的相同 candidate activation 不能被认领；
- builder key 撤销保留 deployment fact，同时动态撤销 active deployment authority；
- rollout control pause 保留 deployment fact，同时动态撤销 active deployment authority；
- source Intent 删除/损坏会撤销 deployment fact authority；
- Store 独立重验 exact stored Intent 与 ARC-07 activation history；
- Engine/public lazy exports 已接线；
- 3 个 5f2 场景与 1 个 Engine 装配测试通过，ruff/public import 通过；未运行全量测试。

## 当前边界与下一步

- 当前 cohort 仍是单机显式 enrollment，`population_assignment_enforced=false`；
- pointer switch 只决定稳定 launcher 下次启动的版本，本切片没有启动或重启用户进程；
- percentage rollout 需要安装注册、稳定分桶与 exposure accounting，不能复用单机 opt-in Receipt 虚报；
- 下一切片应建立 opt-in runtime launch/health observation 与 completion evidence，再决定是否进入 percentage；
- EVO-05.6b2 仍负责消费 rollback authority，执行兼容回滚并生成独立 Rollback Receipt。
