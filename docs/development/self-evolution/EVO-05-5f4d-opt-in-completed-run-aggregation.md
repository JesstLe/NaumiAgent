# EVO-05.5f4d Opt-in Completed-run Aggregation

## 目标

把 EVO-05.5f4c 中同一 candidate completion 的不同 Execution Outcome 动态重验并聚合为独立的 opt-in Stage
Completion Evidence。该证据只在当前 passing liveness、current Rollout Plan、可信 GREEN Baseline 和全部 Outcome source
同时有效时开放 `opt_in_stage_completion_authority`；它仍不签发 percentage stage entry、部署或 promotion 权限。

## Exact 输入与边界

每次 assessment 必须读取并核对：

1. EVO-05.5f4b 当前 passing 的最新 liveness Window；
2. Window 内冻结的 opt-in Cohort、Rollout Plan ID/digest 和第二阶段完整阈值；
3. EVO-05.5a 当前可用的 immutable GREEN Baseline；
4. 同一 completion 下按完成时间排序、最多 100 条的全部 EVO-05.5f4c Outcome；
5. 每条 Outcome 的动态 View，而不是只相信历史 receipt 上的 success boolean。

只有当前仍具备 `execution_outcome_authority` 的不同 run ID 进入分母；其中仍具备
`successful_completed_run_authority` 的 run 进入成功计数。Outcome source 超过 100 条时明确拒绝 assessment，避免静默截断。

## 指标与机械状态

Evidence 冻结 Outcome ID/digest/run ID、当前 authoritative/successful 子集、逐 run duration 与 reported cost，并计算：

- observed/successful/unsuccessful runs；
- error rate 与 completion rate；
- 相对 GREEN Baseline 的 completion-rate drop；
- p95 duration 与 latency regression；
- mean reported cost 与 cost regression。

`RunUsage.reported_cost_usd` 来自 Engine counter delta，固定 `billing_authority=false`。只有 GREEN Baseline 的
`cost_source=live_evidence` 时成本才可比较；`no_model_execution` 零成本基线不能与真实模型运行成本比较，Evidence 必须保留
`cost_baseline_not_comparable`，不得伪造通过。

状态优先级与 local-canary monitor 一致：

1. 任一错误率、完成率下降、p95 延迟或可比成本超阈值：`breached`；
2. 无 breach 但 completed runs 不足或成本基线不可比：`insufficient`；
3. 其余：`passing`。

passing 才开放 opt-in Stage Completion authority；breached 只开放 pause/rollback input。percentage、stable、promotion 和
next-stage-entry authority 全部固定 false。

## 持久化与动态撤权

- Artifact 使用 canonical JSON、SHA-256 content identity 和独立 source-set digest；
- Store 与 Plan/Baseline/Window/Outcome 共用 SQLite evidence DB，`BEGIN IMMEDIATE` 内再次核对所有 durable rows；
- Store 写入前还会动态 inspect Plan、Baseline、Window 和每个 Outcome，并重新核对阈值、原始 duration/cost 与 authority 子集；
- 相同 source snapshot 的并发 assessment 复用同一 Evidence，不生成无意义时间戳历史；
- View 每次重新核对 durable row、latest assessment、Plan、Baseline、当前 liveness 和完整 Outcome set；
- 新 Outcome、单次 Usage/Receipt/coverage 篡改、liveness breach、Plan/Baseline 漂移或更新 assessment 都会即时撤权。

## 验收结果

- 真实 Candidate → Deployment → Health → Liveness → 10 个 managed ChatRun → Outcome → Aggregation 链路通过；
- 6 个并发 assessment 收敛为同一 Evidence；
- 当前 GREEN fixture 为 `no_model_execution` 时精确返回 cost baseline 不可比较，不冒充 passing；
- 伪造 live cost baseline 的 passing Receipt 被 Store 动态 source check 拒绝；
- 新增 failed Outcome 后超出 error-rate threshold，形成 breach 与 pause/rollback input；
- 新 assessment 使旧 Evidence 失去 latest authority；篡改单次 Usage 后 breach authority 动态撤销；
- Evidence digest 被篡改时读取失败关闭；ruff、compile、公共 lazy import 与相关小模块测试通过；未运行全量测试。

## 当前边界与下一步

EVO-05.5f4d 已闭合内部 opt-in Stage Completion Evidence，但当前产品入口尚未展示或执行它。下一最小切片应先实现
`EVO-05.5f4e Opt-in Stage Advance Authorization`：消费 current passing 5f4d Evidence，结合 stage 的
`manual_advance_required` 和 durable user interaction 签发 exact `opt_in → percentage` authority；它仍不得直接部署或扩大
population。随后再把 assessment/advance 状态统一接入 `/evolution`、New UI/TUI 与自动调度，避免 UI 读取历史 receipt 后误报完成。
