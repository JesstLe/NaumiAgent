# EVO-05.5f5e Percentage Runtime Launch and Exposure Receipt

## 目标

消费 current [EVO-05.5f5d](EVO-05-5f5d-percentage-activation-reconciliation.md) Deployment Receipt，并组合
[ARC-07.5e](../architecture/ARC-07-5e-terminal-runtime-identity.md) managed terminal identity、
[HAR-10.2i](../harness/HAR-10-2i-runtime-release-identity-binding.md) release binding 与
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) startup observation chain，形成 exact selected
installation 的 `EvolutionRevalidationPercentageRuntimeExposureReceipt`。

stable launcher 和正常 New UI/TUI 启动路径已经负责创建真实 terminal process。5f5e 不再启动第二个 probe，也不接受任意 argv；
它只在该 runtime 已形成 sequence 1 `starting` → sequence 2 `running` 的 durable 证据后记录 installation-level exposure。

本 Receipt 不证明用户已经发送消息、模型调用成功、工具执行完成或 percentage cohort 整体健康。

## Exact 来源链

`EvolutionRevalidationPercentageRuntimeExposureService.record()` 只接受严格 Assignment ID 与 runtime subject ID：

1. 读取 exact 5f5d Deployment View，要求 `percentage_runtime_launch_input_authority=true`；
2. 按 subject ID 从 Harness Store 读取 exact Runtime Release Binding；
3. 要求 binding 的 Runtime Identity 精确匹配 5f5d active pointer、candidate slot/version/target、Boot Receipt、binary、runtime path
   与 install root；
4. 从 observation ledger 的 origin 开始读取，拒绝任意 cursor；
5. sequence 1 必须是 `chain_origin=startup`、`phase=starting`、无 previous digest；
6. sequence 2 必须是 `phase=running`，并以 sequence 1 sample SHA-256 作为 previous digest；
7. binding、两个 sample 的 workspace/surface/subject/instance/epoch/runtime identity 必须完全一致；
8. 形成 Receipt 后再次重验 5f5d authority；
9. Store 再次读取 Harness exact sources，并在 Evolution DB `BEGIN IMMEDIATE` 内重读 exact durable Deployment Receipt；
10. 同一 Deployment 只允许登记一条选定 binding，重复同一请求幂等，换 subject 不能覆盖。

调用方不能提交 pointer、slot、runtime identity、heartbeat phase、exposure timestamp 或任意命令覆盖服务端来源。

## 为什么不是 health probe

5f5c 的 `--version` 和 opt-in 5f3 的 `--runtime-health-check` 都是短生命周期机器探测，不能证明正常 terminal runtime 已启动。
5f5e 使用 New UI/TUI 生产 `TerminalRuntimeLifecycle` 的启动事实：

- Runtime Identity 声明 `invocation_kind=terminal_session`、`runtime_process_started=true`；
- Binding 与 sequence 1 starting heartbeat 在同一 Harness transaction 内落盘；
- producer 只有在 sequence 2 running 成功后才完成 lifecycle `start()`；
- observation chain 断裂、legacy snapshot、仅 starting、其他 release identity 或其他 workspace 均失败关闭。

因此 Receipt 表示“该 selected installation 的 candidate terminal runtime 已达到 ready boundary”，而不是“候选二进制又通过了一次
probe”。

## Receipt 与权限层次

Receipt 冻结完整 5f5d Deployment、Binding、startup/ready observations、surface 与 ready time，并固定：

- `installation_exposure_observed=true`；
- `runtime_process_started=true`、`terminal_session_process=true`；
- `user_request_executed=false`、`completed_run_authority=false`；
- percentage/stable/promotion authority 均为 false。

动态 View 分三层：

1. `runtime_exposure_fact_authority`：Receipt、5f5d deployment fact、binding 与 startup pair 的 durable source 全部 exact；
2. `current_runtime_exposure_authority`：fact 之外，5f5d active launch input 仍 current，latest heartbeat 仍属于 exact
   instance/epoch、sequence 不回退、phase 为 running/waiting 且未 stale；
3. `percentage_observation_input_authority`：与 current exposure 相同，只允许下一层聚合持续 observation，不是 rollout completion。

runtime 正常 stopped/failed/stale、pointer rollback、slot/boot 漂移会撤销 current authority，但不会否认已经发生的 startup fact。
Harness retention 或 ledger/binding 篡改会使历史 source 不可验证，从而撤销 fact authority。

## 安全与数据边界

- SQL 全部 parameterized，动态列名不存在；
- subject ID 使用 Harness 同一闭集格式；
- Receipt 不保存环境变量、argv、API key、模型凭据、用户输入、聊天正文或 heartbeat owner；
- 不通过 shell，不发起网络请求，不启动额外进程；
- Artifact 上限 16 MiB，nested content identity 全部重算；
- Heartbeat 健康使用 HAR `assess_heartbeat()` authority clock 计算，前端不能自行延长 timeout。

## 验收结果

- 真实 Evolution 5f5a→5f5d 链与生产 TerminalRuntimeLifecycle/Harness SQLite 路径组合运行；
- managed New UI runtime 的 exact starting/running pair 形成 Exposure Receipt，重复记录幂等；
- lifecycle stopped 后 exposure fact 保留，current/observation input authority 即时撤销；
- 其他 managed release 即使拥有合法 binding，也不能归属当前 Percentage Deployment；
- 只有 starting、没有 running 的 subject 不产生 Receipt；
- ready observation JSON 摘要篡改后 Harness 读取失败关闭，Exposure fact/current authority 全部撤销；
- ruff、compile、public lazy import 与本模块 3 个小场景通过；未运行全量测试。

## 当前不足与下一步

本机测试使用真实 ReleaseSlot/Harness Store/TerminalRuntimeLifecycle 代码与 POSIX executable fixture，但没有从打包后的 `naumi`
launcher 启动一个完整交互式子进程；Windows 真 `.exe` 与 packaged New UI/TUI 启动仍需三平台 CI 做端到端证明。

5f5e 只形成单 installation 的一条 exact startup exposure；它没有跨所有 Harness subject 排序，因此不声称这是全局时间上
最早的 runtime。下一最小切片为 `EVO-05.5f5f Percentage Runtime Observation Window`：消费
current Exposure、后续 append-only heartbeat samples 与 Plan guardrails，机械检查 minimum duration、sample count、maximum gap、
pointer/exposure continuity；它仍不能把单 installation window 冒充整个 cohort 的 rollout completion。
