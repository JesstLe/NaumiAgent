# EVO-05.5f5c Percentage Boot Preparation

## 目标

只消费 current [EVO-05.5f5b](EVO-05-5f5b-percentage-deployment-intent.md) Percentage Deployment Intent，
对 exact immutable candidate slot 执行真实 `naumi-runtime[.exe] --version` 探测，并冻结独立、可动态撤权的
`EvolutionRevalidationPercentageBootPreparation`。

本切片证明候选 runtime bytes 在 selected managed installation 的当前主机上可以启动并报告 signed manifest version。
它不切换 active pointer、不启动用户 session、不形成 Deployment Receipt，也不声明真实 percentage exposure 已发生。

## Exact 执行链

`EvolutionRevalidationPercentageBootPreparationService.prepare()`：

1. 按 Assignment ID 读取 exact 5f5b Intent，并要求全部动态 authority 仍 current；
2. 在 Evolution evidence DB 为 Intent 领取跨进程 claim；
3. 领取后再次重验 Intent、candidate slot、host target 与 previous pointer；
4. 只调用 ARC-07 `ReleaseSlotStore.verify_bootable()`，命令固定为 slot 内 backend 加 `--version`；
5. 不接受 shell、用户命令、额外参数、工作目录或环境覆盖；
6. 输出上限 64 KiB、UTF-8 文本上限 4096 字符，manifest version 必须是独立 token；
7. 默认超时 20 秒，可由内部执行器收窄到 1..20 秒，但不能扩大；
8. probe 返回后再次重验 exact Intent 和 previous pointer；
9. 只有 current Boot Receipt、slot、pointer 与 Intent 全部一致才写 Prepared Receipt。

ARC-07 现在把 `subprocess.TimeoutExpired` 明确映射为 `release_slot_boot_timeout`，5f5c 再映射为
`percentage_boot_probe_timeout`；错误版本、非零退出、无效 UTF-8、过大输出和执行失败统一 fail closed，均不产生 Prepared
authority。

## Claim、epoch 与崩溃恢复

进程内锁不足以保护多个 Naumi 进程，因此 5f5c 使用 SQLite durable claim：

- `intent_id` 是唯一 claim key；
- claim 冻结 owner、epoch、started time、lease expiry 和状态；
- 默认 lease 30 秒，覆盖最大 20 秒 probe 与收口余量；
- 活跃 lease 期间其他 owner 只等待，不执行第二个 probe；
- 执行者崩溃后，lease 到期允许新 owner 以 `epoch + 1` 接管；
- 旧 owner 即使恢复，也因 owner/epoch/lease CAS 不匹配而无法写 Prepared Receipt；
- probe 明确失败时 exact owner 删除 claim，允许同一 Intent 在剩余窗口内受控重试；
- Prepared Receipt 插入与 claim `completed` 在同一 `BEGIN IMMEDIATE` 事务内原子提交；
- 四个独立 Service 并发最终只执行一次真实 probe，并返回同一个 artifact。

等待上限为 35 秒，覆盖 30 秒 lease，使同批等待者可以在崩溃后直接跨过 lease 边界接管；不会使用无界等待。

## Prepared Receipt

Artifact 冻结：

- 完整 Percentage Deployment Intent；
- ARC-07 Boot Receipt 及 slot/manifest/binary/output digest；
- claim owner/epoch；
- boot started/prepared timestamp；
- fixed execution method 与边界布尔值。

模型机械要求 Boot Receipt：

- slot ID/digest 与 Intent candidate 完全一致；
- manifest digest 与 Intent 完全一致；
- arguments 只能是 `("--version",)`；
- exit code 为 0、version matched、bootable 和 activation input authority 均为 true；
- claim start 不晚于 probe checked time，probe checked time 不晚于 Prepared time，且 Prepared 在原 Intent 过期前形成。

`boot_executed=true` 和 `probe_process_started=true` 只表示短生命周期版本探测确实发生；
`user_process_started=false`、`process_started=false` 表示没有启动 Naumi 用户 runtime/session。

## Durable Store 与动态撤权

Store 与 5f5b Intent 共用 Evolution SQLite DB，并在写前、事务内和 View 读取时分别重验：

- durable exact Intent JSON；
- current 5f5b authority 和有效期；
- exact claim owner/epoch/lease；
- installed slot bytes、immutability 与 target；
- Boot Receipt 对 current backend binary digest 的绑定；
- previous active pointer 仍等于 Intent CAS baseline；
- candidate slot 仍 inactive。

Prepared artifact 限制 10 MiB，全部 SQL parameterized，JSON restore 会重算 nested identity。Intent 过期、pointer 推进、slot/binary
篡改、Boot Receipt 损坏或 Prepared JSON 篡改都会保留历史审计但关闭
`percentage_activation_input_authority`。

## 权限边界

current View 只产生 `percentage_activation_input_authority=true`，含义是“允许下一层尝试 exact pointer CAS”。以下能力始终为
false：

- `active_pointer_switched`、`activation_authority`；
- `deployment_receipt_authority`；
- `process_started`；
- `percentage_rollout_authority`、`stable_rollout_authority`、`promotion_authority`。

调用方不能把 Boot Receipt 或 Prepared Receipt 单独解释为 deployment、exposure、健康窗口或 stage completion。

## 验收结果

- 真实 selected Assignment、5f5b Intent、signed archive、inactive candidate 和 active baseline 链路运行；
- 四个独立 Service 并发只启动一次真实 candidate `--version` probe，并返回同一 Prepared Receipt；
- 模拟 owner 崩溃后，新 owner 以 epoch 2 接管并完成；
- 错误版本/非零退出失败关闭并释放 claim，不留下 Prepared Receipt；
- 1 秒真实 subprocess timeout 产生专用错误并释放 claim；
- probe 期间 pointer 被推进会在写入前阻断 Prepared Receipt；
- Intent 过期、后续 pointer 漂移、candidate bytes 篡改均动态撤权；
- Prepared JSON 篡改在 restore 时 fail closed；
- ruff、compile、public lazy import 与本模块 5 个真实场景通过；未运行全量测试。

## 当前边界与下一步

5f5c 已证明 exact candidate 在 current installation 上可启动，但 active pointer 仍指向 previous slot。
[EVO-05.5f5d](EVO-05-5f5d-percentage-activation-reconciliation.md) 已消费 current Prepared Receipt，以 5f5b 冻结的
`expected_previous_pointer_sha256` 执行 ARC-07 authority-bound atomic CAS，并覆盖“pointer 已切换但 Receipt 尚未落盘”的
崩溃窗口。真实 runtime launch、exposure accounting 与健康观察仍留给后续切片。
