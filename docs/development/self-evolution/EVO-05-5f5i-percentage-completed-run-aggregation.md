# EVO-05.5f5i Percentage Completed-run Aggregation

## 目标

把同一 percentage Assignment 下不同真实 run 的
[EVO-05.5f5h](EVO-05-5f5h-percentage-release-bound-execution-outcome.md) Outcome 动态重验并聚合为独立的
`EvolutionRevalidationPercentageStageCompletion`。该证据只有在 current passing Window、current Rollout Plan、可信 GREEN
Baseline 和完整 Outcome source set 同时有效且所有机械 guardrail 通过时，才开放 percentage stage-completion authority。

本切片不签发 `percentage → stable` entry、不扩大 stable 流量，也不形成 promotion authority。

## Exact source set

每次 assessment 必须读取并绑定：

1. 5f5g 当前 passing 的 latest Percentage Observation Window；
2. Window 内冻结的 Assignment、Deployment、candidate release binding、population denominator、member rank 与 exposure percent；
3. current Rollout Plan 的第三阶段完整阈值；
4. EVO-05.5a current GREEN Baseline；
5. 同一 Assignment 下按 completion time 排序、最多 100 条的完整 5f5h Outcome set；
6. 每条 Outcome 的动态 View，而不是历史 receipt 上的 success boolean。

只有 current `execution_outcome_authority=true` 的不同 run ID 进入分母；completed success 进入成功计数，partial、failed、cancelled
进入失败计数。Outcome 超过 100 条时明确拒绝 assessment，不能通过 `LIMIT 100` 静默截断。

## 机械指标与状态

共享 typed metric kernel 从 authoritative Outcome 计算：

- observed/successful/unsuccessful runs；
- error rate 与 completion rate；
- 相对 GREEN Baseline 的 completion-rate drop；
- nearest-rank p95 duration 与 latency regression；
- mean reported cost 与 cost regression。

所有比率使用整数 basis points，duration 使用整数 microseconds，成本使用整数 micro-USD，避免浮点漂移。reported cost 仍固定
`billing_authority=false`。只有 Baseline `cost_source=live_evidence` 时成本可比；`no_model_execution` 基线必须产生
`cost_baseline_not_comparable`，不得伪造 passing。

状态优先级：

1. 任一 error/completion-drop/p95/cost guardrail 超限：`breached`；
2. 无 breach，但不同 completed run 不足或成本不可比：`insufficient`；
3. 其余：`passing`。

passing 只开放 percentage stage-completion authority；breached 只开放 pause/rollback input。next-stage-entry、stable-rollout 与
promotion authority 始终为 false。

## 持久化、并发与动态撤权

- Artifact 使用 canonical JSON、SHA-256 content identity 与 source-set digest；
- Store 与 Plan/Baseline/Window/Outcome 共用 Evolution evidence DB，JSON 上限 512 KiB；
- `BEGIN IMMEDIATE` 内通过参数化 SQL 重读 exact Plan、Baseline、Window 与完整 Outcome rows；
- 相同 Assignment/source snapshot 的跨 Service 并发 assessment 幂等复用同一 Evidence；
- latest assessment 以 append-only rowid 决定，新 Outcome 会使旧 Evidence 失去 latest authority；
- View 每次动态重验 Plan、Baseline、passing liveness 和每个 Outcome 的 ChatRun/usage/coverage source；
- Assignment、subject、evidence ID 与数量边界在访问 SQLite 或创建锁之前校验。

Evidence 不复制 prompt、工具参数、工具输出、完整 assistant 正文、API key 或环境变量。

## 验收结果

- 真实 5f5a→5f5h、TerminalRuntimeLifecycle、Harness SQLite、30 个 managed ChatRun 与 Outcome 完成聚合；
- 六路跨 Service 并发 assessment 收敛为同一 content identity；
- 30/30 成功但 GREEN fixture 为 `no_model_execution` 时诚实保持 insufficient；
- 第 31 个 failed Outcome 超出 percentage error-rate threshold，形成 breach 与 pause/rollback input；
- 新 assessment 使旧 Evidence 失去 latest/outcome-set authority；
- 单次 Run Usage source 被篡改后 breach authority 动态撤销；durable Evidence digest 被篡改后读取失败关闭；
- 非法 Assignment 输入在查询前被拒绝；ruff、compile、公共 lazy import、YAML 与单个真实小模块测试通过；未运行全量测试。

## 当前边界与下一步

5f5i 已形成 percentage 阶段完成证据，但当前真实 fixture 因成本基线不可比不会虚报 passing。
[EVO-05.5f5j](EVO-05-5f5j-percentage-to-stable-stage-advance.md) 已消费 current passing 5f5i、exact Plan/control state 与
stable mandatory manual policy，通过 durable user interaction 签发短期、一次性的 stable entry authority。在后续 stable
deployment 完成前，任何 UI 都不得显示“已进入稳定发布”。
