# EVO-05.5f5m Stable Activation Reconciliation

## 目标

只消费 current [EVO-05.5f5l](EVO-05-5f5l-stable-boot-preparation.md) Stable Prepared Receipt，使用
[ARC-07.5a](../architecture/ARC-07-5a-installed-version-slots.md) authority-bound expected-pointer CAS 将 exact managed stable
installation 原子切换到 candidate slot，并形成可从 immutable activation history 机械恢复的
`EvolutionRevalidationStableDeploymentReceipt`。

本切片只证明本机 stable launcher pointer 已切换。它不启动用户 runtime、不记录 stable installation exposure，也不声明
100% population 已完成 stable rollout 或 promotion。

## 最小 ARC 前置

ARC-07 v2 activation authority 的闭集增加一个独立成员：

- `kind=evolution_stable_boot_preparation`；
- `authority_id=evrestableboot_<24 hex>`；
- `authority_sha256` 必须是 exact Stable Prepared Receipt digest。

kind 与 ID prefix 由 `ReleaseActivationAuthority` 交叉校验。Stable authority 不能伪装为 opt-in 或 percentage authority；v1
pointer、既有 v2 pointer 与 rollback 禁止携带 authority 的行为保持兼容。

## 执行链

`EvolutionRevalidationStableDeploymentService.deploy(intent_id=...)`：

1. 只接受严格 `evrestableintent_*`，按 Intent 幂等读取既有 Stable Deployment Receipt；
2. 从同一 Evolution evidence DB 读取 exact durable Intent 与 Stable Prepared Receipt；
3. 先查询 Intent 冻结的 expected activation generation；若已经存在，只允许 exact Prepared authority、previous pointer、
   candidate slot 与 Boot Receipt 全部匹配；
4. 若尚未激活，调用 5f5l `inspect()`，要求 current `stable_activation_input_authority`；
5. 重读 active pointer，必须完全等于 Intent 冻结的 previous pointer，且 Intent 尚未过期；
6. 调用 ARC-07 `activate()`，同时传入 expected pointer digest 与 exact Stable Prepared authority；
7. 验证 generation、hash-chain previous link、slot identity、Boot Receipt、atomic switch 与 old-slot retention；
8. 在 `BEGIN IMMEDIATE` 事务内重读 durable Preparation 与 Intent，并幂等写入 content-addressed Receipt；
9. 返回动态 View，不因一次 pointer 写入推导 runtime 或 rollout 事实。

所有 SQL 均参数化。调用方不能指定 slot、generation、pointer digest、authority kind、activation time 或 Boot Receipt。

## 并发与崩溃恢复

`deploy()` 与 `reconcile()` 共用同一状态机：

- 多个 Service 同时观察旧 pointer 时，ARC-07 的事务与 expected-pointer CAS 只允许一次切换；
- CAS 竞争失败后重读 expected generation；只有 exact Stable Prepared authority 才能收敛到同一 Receipt；
- generation 被无 authority、percentage authority 或其他 Stable Prepared 占用时 fail closed；
- pointer 已切换但 Receipt 写入失败时，从完整 activation hash-chain 重建同一 content identity，不重复切换 pointer；
- Intent 后续过期或 pointer 已 rollback 时，只要历史 activation 在原 authority window 内，仍可补写历史事实；
- 恢复后的历史 Receipt 不会被误标为 active deployment 或 runtime launch input。

## Receipt 与权限分层

Receipt 冻结完整 Stable Prepared Receipt、authority-bound pointer 与 activation timestamp，并明确：

- population membership 与 installation proof-of-possession 已在 Intent 中执行；
- local installation pointer 已切换且 old slot 保留；
- `process_started=false`、`user_process_started=false`；
- `stable_installation_exposure_observed=false`；
- percentage/stable rollout 与 promotion authority 均为 false。

动态 View 分为三层：

1. `deployment_fact_authority`：Receipt、Preparation、Intent durable source 与 activation chain 全部 exact；
2. `active_deployment_authority`：历史事实之外，pointer 仍为 active tail，candidate bytes 与 Boot Receipt 仍 current；
3. `stable_runtime_launch_input_authority`：active 之外，Stage Advance、Plan、Population Snapshot、installation credential、Archive
   Admission、host target 与 Intent TTL 仍 current，才允许下一层尝试 runtime startup。

Intent expiry、后续 rollback 或 pointer 推进只撤销 active/launch authority，不抹除真实历史 activation；durable source 或
activation chain 损坏则撤销 fact authority。

## 验收证据

- 真实 signed Population、逐安装 Ed25519 PoP、signed archive、immutable slot、Stable Intent 与真实 `--version` boot probe 全链运行；
- 两个独立 Deployment Service 的八个并发调用收敛到一个 authority-bound generation 与一个 Receipt；
- pointer 切换后注入 Receipt write failure，再 rollback、等待 Intent 过期，`reconcile()` 从 historical generation 恢复同一事实；
- 恢复后的 Receipt 保留 fact authority，但 active/runtime launch authority 为 false；
- Stable Prepared durable JSON 篡改会动态撤销 deployment fact；
- Stable/Percentage authority kind 与 ID prefix 互相伪装时失败关闭，ARC-07 v1/v2 兼容测试通过；
- `tests/unit/test_evolution_revalidation_stable_deployments.py`：`1 passed`；
- ARC authority 定向测试：`1 passed, 8 deselected`；未运行全量测试。

本机真实 boot fixture 使用 POSIX shebang；Windows schema/target 仍由 ARC-07 既有单测覆盖，真实 `.exe` stable activation 必须由
Windows runner 验收。

## 当前边界与下一步

5f5m 已完成 exact stable installation 的可对账 activation。下一最小切片是 Stable Runtime Exposure：只消费 current
`stable_runtime_launch_input_authority`，绑定 ARC-07.5e Runtime Identity 与 HAR starting/running observation，记录单 installation
startup exposure；它仍不能凭单机启动声明完整 population stable rollout 或 promotion。
