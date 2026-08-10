# EVO-05.5f5l Stable Boot Preparation

## 状态

已实现。

## 目标

消费 current [EVO-05.5f5k](EVO-05-5f5k-stable-deployment-intent.md) Stable Deployment Intent，对 exact immutable
candidate slot 真实执行 `naumi-runtime[.exe] --version`，并形成独立、可动态撤权的 Stable Prepared Receipt。

本切片只证明这一台 managed installation 上的 candidate runtime bytes 可启动并报告 signed manifest version；它不切换 active
pointer、不启动用户 session、不形成 Deployment Receipt，也不声明 stable installation exposure 或 100% rollout 已发生。

## Typed authority 边界

Stable Boot 使用独立的：

- `EvolutionRevalidationStableBootPreparation`；
- `EvolutionRevalidationStableBootPreparationStore/Service/View`；
- `evolution_revalidation_stable_boot_preparations` 与 claim 表；
- `stable_activation_input_authority`。

它不修改字段或反序列化为 Percentage Prepared Receipt。Service 只接受 `evrestableintent_*`，通过 5f5k 的 typed
`inspect_intent()` 重验完整 Stable Intent；`inspect_intent()` 只是按 content-addressed ID 读取既有 artifact，不签发新权限。

## 固定 probe 与进程安全

执行器只调用 ARC-07 `ReleaseSlotStore.verify_bootable()`：

1. executable 必须来自 Intent exact immutable slot；
2. arguments 固定为 `("--version",)`；
3. 不接受 shell、用户命令、额外参数、cwd 或 environment override；
4. 默认 timeout 20 秒，内部只允许收窄到 1..20 秒；
5. ARC-07 限制 stdout/stderr、UTF-8 与版本 token，并将 timeout 作为稳定错误码；
6. probe 前后重验 complete Stable Intent、slot、host target 与 previous pointer；
7. probe 成功不等于 user process、deployment、exposure 或 rollout。

## Claim、lease 与崩溃恢复

- `intent_id` 是跨进程唯一 claim key；
- claim 冻结 owner、epoch、started time、lease expiry 与 `claimed/completed` 状态；
- 默认 lease 30 秒、等待最多 35 秒，覆盖 20 秒最大 probe 且没有无界等待；
- active lease 下其他 owner 只等待；lease 过期后以 `epoch + 1` 接管；
- 旧 owner 恢复后因 owner/epoch/lease CAS 不一致无法写 Prepared Receipt；
- 明确 probe 失败会删除 exact claimed row，允许 Intent 有效期内受控重试；
- Prepared 插入与 claim completed 在同一 `BEGIN IMMEDIATE` 事务原子提交。

## Durable Receipt 与动态撤权

Prepared Receipt 冻结完整 Stable Intent、ARC-07 Boot Receipt、claim owner/epoch、boot start/prepared timestamp 和全部负权限。
Store 使用 parameterized SQL，artifact 限制 10 MiB，并在写前、事务内与 View 阶段重验：

- exact durable Stable Intent 与全部 current source；
- exact claim owner/epoch/lease；
- installed slot bytes、manifest、immutability 与 target；
- Boot Receipt 对 backend binary digest、`--version` 与 signed version 的绑定；
- previous pointer 仍等于 Intent CAS baseline，candidate 仍 inactive；
- Intent TTL。

Intent/slot/pointer 漂移、Boot Receipt 损坏、Prepared JSON 篡改或 TTL 到期会保留历史审计，但关闭
`stable_activation_input_authority`。

## 权限边界

current View 只开放 `stable_activation_input_authority=true`，含义是下一层可以尝试 exact pointer CAS。以下保持 false：

- `active_pointer_switched`、`activation_authority`；
- `deployment_receipt_authority`、`process_started`；
- `percentage_rollout_authority`、`stable_rollout_authority`、`promotion_authority`。

## 验收结果

- 真实 5f5j→5f5k、Registry-signed Population、四个 installation identity、signed archive、inactive candidate 与 bootable active
  baseline 链路完成；
- 四个独立 Service 并发只执行一次真实 candidate `--version`，收敛到同一 Prepared Receipt；
- 第二 installation 的 crashed owner lease 到期后由 epoch 2 owner 接管；
- timeout 映射为专用错误并删除 claim，不留下 Prepared Receipt；
- probe 期间 pointer 被推进会在落盘前阻断；
- Prepared JSON 篡改 fail closed，Intent TTL 到期动态撤权；
- ruff、compile、public lazy import、YAML 与单个真实小模块测试通过；未运行全量测试。

## 当前边界与下一步

5f5l 已证明 candidate 可在 exact stable installation 上启动，但 active pointer 仍指向 previous slot。下一最小切片是 Stable
Activation/Reconciliation：消费 current Prepared Receipt，以 5f5k frozen previous pointer 执行 authority-bound CAS，并机械对账
“pointer 已切换但 Deployment Receipt 尚未写入”的崩溃窗口；仍不直接声明 stable rollout 完成。
