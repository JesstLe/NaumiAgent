# EVO-05.5f5n Stable Runtime Exposure

## 目标

消费 current [EVO-05.5f5m](EVO-05-5f5m-stable-activation-reconciliation.md) Stable Deployment，组合
[ARC-07.5e](../architecture/ARC-07-5e-terminal-runtime-identity.md) managed runtime identity、
[HAR-10.2i](../harness/HAR-10-2i-runtime-release-identity-binding.md) release binding 与
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) startup observation chain，形成单个 stable
installation 的 `EvolutionRevalidationStableRuntimeExposureReceipt`。

本切片只确认 production TerminalRuntimeLifecycle 已在 exact stable candidate 上到达 ready boundary。它不启动第二个 probe，
不接收调用方 argv，也不证明用户请求、完整 stable population、长期健康、rollout completion 或 promotion。

## Exact 来源链

`EvolutionRevalidationStableRuntimeExposureService.record()` 只接受严格 Stable Intent ID 与 runtime subject ID：

1. 读取 exact 5f5m Deployment View，要求 `stable_runtime_launch_input_authority=true`；
2. 从 Harness Store 读取 subject 的 exact Runtime Release Binding；
3. Runtime Identity 必须逐字段匹配 active pointer、candidate slot/version/target、Boot Receipt、binary、runtime path 与 install root；
4. observation 必须从 sequence 1 origin 读取，调用方不能提交 cursor；
5. sequence 1 必须为 `starting` 且无 previous digest；sequence 2 必须为 `running` 且链接 sequence 1 digest；
6. binding 与两个 observation 的 workspace、surface、subject、instance、epoch 和 runtime identity 必须完全一致；
7. Receipt 形成后再次重验 5f5m launch authority；
8. Store 在 `BEGIN IMMEDIATE` 中重读 exact durable Stable Deployment，并再次读取 Harness source；
9. 同一 Stable Intent/Deployment 只能绑定一个 runtime subject，重复 exact 请求幂等，换 subject 冲突关闭。

调用方不能提交 pointer、slot、identity、heartbeat phase、ready time、环境变量或命令覆盖服务端事实。

## Receipt 与动态 authority

Receipt 固定：

- `stable_installation_exposure_observed=true`；
- `runtime_process_started=true`、`terminal_session_process=true`；
- `user_request_executed=false`、`completed_run_authority=false`；
- `stable_observation_input_authority=true` 只表示 artifact 的下游用途，动态使用仍由 View 判定；
- percentage/stable rollout 与 promotion authority 均为 false。

View 分三层：

1. `runtime_exposure_fact_authority`：Receipt、Deployment fact、Binding 和 startup pair 的 durable source 全部 exact；
2. `current_runtime_exposure_authority`：fact 之外，Stable Deployment launch input 与 runtime heartbeat 仍 current；
3. `stable_observation_input_authority`：与 current exposure 相同，只允许下一层建立持续 observation window。

runtime stopped/failed/stale、pointer 变化或 Intent 过期只撤销 current/input authority，不否认已发生的 startup fact。Harness
retention、Binding/ledger 篡改或 Deployment source 损坏会撤销 fact authority。

## 安全与并发边界

- 所有 SQL 参数化；唯一索引覆盖 deployment、intent 与 binding；
- 两个独立 Service 的并发写在 SQLite `BEGIN IMMEDIATE` 中收敛为同一 content identity；
- Artifact 限制 16 MiB，Pydantic 重建所有 nested model 并复算 Receipt digest；
- 不保存 API key、环境变量、argv、用户输入、聊天正文或 heartbeat owner；
- 不发起网络请求，不执行 shell，不启动额外进程；
- heartbeat health 使用 HAR authority clock，不允许 UI 或调用者延长 timeout。

## 验收证据

- 真实 signed Population → Stable Intent → boot → authority-bound activation 全链运行；
- 生产 `TerminalRuntimeLifecycle` 形成 exact starting/running pair，八路跨 Service 并发收敛并支持幂等读取；
- lifecycle stopped 后历史 fact 保留，current 与 observation input authority 撤销；
- 合法但属于其他 release 的 managed runtime 不能绑定当前 Stable Deployment；
- 只有 starting、没有 running 时不签发 Receipt；
- ready observation JSON 篡改后动态撤销 fact/current authority；
- public lazy import、Ruff、compile 与本模块 3 个真实小场景通过；未运行全量测试。

## 当前边界与下一步

本机 fixture 使用真实 ReleaseSlot、Stable Deployment、Harness Store 与 TerminalRuntimeLifecycle，但 executable 是 POSIX fixture；
Windows 真 `.exe` 与 packaged New UI/TUI 启动仍需三平台发布 CI 验收。

下一独立切片应建立 Stable Runtime Observation Window：从本 Exposure origin 有界读取 heartbeat hash chain，按 Rollout Plan 的 stable
门槛验证持续时长、样本数、最大 gap 与 stale 状态。单 installation window 仍不得冒充完整 population stable completion。
