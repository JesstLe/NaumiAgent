# EVO-05.5f5r Stable Completed-run Aggregation

## 目标

把同一 Stable Intent、同一安装成员下不同真实 run 的
[EVO-05.5f5q](EVO-05-5f5q-stable-release-bound-execution-outcome.md) Outcome 动态重验并聚合为独立的
`EvolutionRevalidationStableStageCompletion`。只有 current passing Window、current Rollout Plan、可信 GREEN Baseline、exact
Population lineage 和完整 Outcome source set 同时有效，且所有机械 guardrail 通过时，才开放该安装成员的 stable-stage
completion authority。

本切片不把单安装成员完成冒充完整 Population rollout，不形成 stable-rollout 或 promotion authority。

## Exact source set

每次 assessment 必须读取并绑定：

1. 5f5p 当前 passing 的 latest Stable Observation Window；
2. Window 内 exact Stable Intent、Exposure、Deployment、Binding、candidate release；
3. Population Snapshot ID、SHA-256、sequence、denominator 与 installation member ID；
4. current Rollout Plan 的第四阶段完整阈值和固定 `stable/100%` exposure；
5. EVO-05.5a current GREEN Baseline；
6. 同一 Intent 下按 completion time 排序、最多 100 条的完整 5f5q Outcome set；
7. 每条 Outcome 的动态 View，而不是历史 receipt 上保存的 authority boolean。

只有 current `execution_outcome_authority=true` 的不同 run ID 进入指标分母；completed success 进入成功计数，partial、failed、
cancelled 进入失败计数。Outcome 超过 100 条时 assessment 明确失败关闭，不能静默截断。

## 指标、状态与 authority

复用 typed stage-completion metric kernel，从 authoritative Outcome 机械计算：

- observed、successful、unsuccessful runs；
- error rate、completion rate 与相对 GREEN Baseline 的 completion-rate drop；
- nearest-rank p95 duration 与 latency regression；
- mean reported cost 与 cost regression。

所有率使用整数 basis points，时长使用整数 microseconds，成本使用整数 micro-USD。reported cost 固定
`billing_authority=false`；只有 `cost_source=live_evidence` 的 Baseline 可比较，`no_model_execution` 必须诚实产生
`cost_baseline_not_comparable`。

状态优先级：

1. 任一 error/completion-drop/p95/cost guardrail 超限：`breached`；
2. 无 breach，但 completed run 不足或成本不可比：`insufficient`；
3. 其余：`passing`。

passing 只开放该 installation member 的 `stable_stage_completion_authority`；breached 只开放 pause/rollback input。
`stable_rollout_authority` 与 `promotion_authority` 始终为 false。

## 持久化、并发与动态撤权

- Evidence 使用 canonical JSON、SHA-256 content identity 与 source-set digest；
- Store 与 Plan、Baseline、Window、Outcome 共用 Evolution evidence DB，artifact 上限 512 KiB；
- `BEGIN IMMEDIATE` 内参数化重读 exact Plan、Baseline、Window 与完整 Outcome rows；
- 相同 Intent/source snapshot 的跨 Service 并发 assessment 幂等复用同一 Evidence；
- latest assessment 由 append-only rowid 决定，新 Outcome 会让旧 Evidence 失去 latest/outcome-set authority；
- View 每次动态重验 Plan、Baseline、stable Window 和每个 Outcome 的 ChatRun、usage、Binding、heartbeat coverage；
- Intent、subject 与 evidence ID 在访问 SQLite 或创建锁前先做语法和长度校验。

Evidence 不复制 prompt、工具参数、工具输出、完整 assistant 正文、API key 或环境变量。

## 验收结果

- 真实 Stable Intent → Deployment → Runtime Exposure → durable Window → 80 个 managed ChatRun → Outcome → 聚合链路通过；
- 六路跨 Service 并发 assessment 收敛为同一 content identity；
- 80/80 success 达到 stable `minimum_completed_runs`，但真实 fixture 的 `no_model_execution` GREEN Baseline 使状态诚实保持
  insufficient，而不是伪造 passing；
- 第 81 个 failed Outcome 超出 error-rate threshold，形成 breach 与 pause/rollback input；
- 新 assessment 使旧 Evidence 失去 latest 与 outcome-set authority；
- ChatRun usage source 漂移后 breach authority 动态撤销，durable digest 篡改后读取失败关闭；
- 非法 Intent 输入在查询前拒绝；ruff、compile、公共 lazy import、YAML 与单个真实小模块测试通过；未运行全量测试。

## 当前边界与下一步

5f5r 证明的是 exact Stable Intent 对应的单 installation member 完成了足够真实运行，并未证明 Population Snapshot 中全部成员均已
完成 stable exposure、observation 与 completed-run guardrail。[EVO-05.5f5s](EVO-05-5f5s-stable-population-candidate-preview.md)
已先交付 durable candidate 的跨安装成员只读聚合/展示，并明确固定无 rollout/promotion authority。下一步必须补生产动态
重验组合与 current trusted Population 对账；不得直接把历史 Evidence 接到 promotion，也不得沿自进化编号无界展开。
