# HAR-09.6b Before/After Outcome Evidence

## 目标

把 Proposal 实施前后的 HAR-08 评测事实绑定到 HAR-09.6a 已存在的 `rolled_back` Outcome，形成可持久、
可复验、可在 Workbench/New UI/TUI 展示的 `implementation_before_after` 证据。本切片只回答：

> 原 Proposal 对应 Candidate 在隔离实验中，相对同源 RED baseline 的 GREEN 结果是什么？

它不回答“回滚后是否恢复”“线上长期是否改善”，也不授予 policy learning、promotion、merge、push 或 publish
authority。

## 语义边界

HAR-08 H5c receipt 本身只绑定 workspace、suite、baseline cohort 与 current cohort，不能单独声称属于某个
Proposal。本切片只接受已经进入 `EvolutionFinalEvaluationReceipt` 的 H5c：Final Evaluation 通过
Aggregation Contract、Validation Plan 和 Candidate 把 Interventional 及全部 Adversarial lane 聚合完整；原
Promotion Input 又同时绑定 Experiment Contract 与该 Final Evaluation。由此形成以下可机械复验的 lineage：

```text
Rollback Outcome
  -> Fresh Revalidation Promotion Input
  -> Prior Promotion Package Input
  -> Experiment Contract / Workbench Proposal / Candidate
  -> Final Evaluation Receipt
  -> Evaluation Lane Receipts
  -> HAR-08 H5c Comparison Receipts
  -> RED baseline cohorts + GREEN candidate cohorts
```

原 Promotion Input 在 revalidation 时已经被标为 `prior_final_invalidated=true`，这意味着它不能被复用为新的
发布批准；它仍然是不可改写的历史实施评测事实。因此界面统一显示“实施前 RED baseline → 实施后 GREEN
candidate”，禁止把它改名为“回滚后评测”。

## Typed Evidence

`EvolutionProposalBeforeAfterEvidence` 是内容寻址的 immutable artifact，包含：

- Outcome、Rollback Request、Workbench Session/Proposal；
- Experiment Contract、Fresh/Prior Promotion Input；
- Candidate identity 与 Final Evaluation Receipt；
- 连续排序的 1 个 Interventional lane 和 1..3 个 Adversarial lane；
- 每个 lane 的 H5c ID/SHA、suite、platform、decision、statistical verdict；
- before/after cohort 的 batch、identity、sample digest、状态计数、case 计数、耗时与可用资源观测；
- 固定安全边界：`post_rollback_evaluation_recorded=false`、`long_term_metrics_recorded=false`、
  `promoted=false`、`learning_authority=false`、`promotion_authority=false`。

Evidence ID 为 canonical JSON SHA-256 的前 24 位，完整 SHA 写入 artifact；Pydantic strict/frozen model 禁止
额外字段、NaN、无时区时间、重复 comparison、lane 缺口和跨 Proposal 绑定。

## 持久化与并发

Evolution 与 Harness 使用不同 SQLite authority store，不能伪装成跨库原子事务。
`EvolutionProposalBeforeAfterEvidenceStore` 仅在 Evolution/session DB 的 `BEGIN IMMEDIATE` 事务内完成：

1. 复验 Outcome ID/SHA；
2. 复验 Fresh/Prior Promotion Input ID/SHA；
3. 复验 Final Evaluation ID/SHA；
4. 以 Outcome、Request 和 Workbench Proposal 为唯一键写入 evidence。

`EvolutionProposalBeforeAfterEvidenceService` 在写入前从独立 Harness DB 复验每个 workspace-scoped H5c
receipt，写入后再完整重载 Outcome、Promotion Input、Final Evaluation 和所有 H5c receipt。若两次读取之间发生
漂移，record 返回 `proposal_before_after_authority_changed`，已写 artifact 也会在后续 inspect 中保持无 authority，
不会被投影为可信结果。这是显式的双库 fail-closed 协议，不声称提供不存在的跨 SQLite 原子性。

同一 artifact 的并发重复调用返回相同结果；同一 Outcome 试图写入不同 artifact 时返回
`proposal_before_after_conflict`。所有 SQL 使用参数绑定，artifact 限制为 512 KiB，ID 使用固定格式，路径要求
canonical absolute workspace。读取时再次执行摘要和 typed model 校验；任何缺失、损坏或漂移均 fail-closed。

## 动态 Authority

`EvolutionProposalBeforeAfterEvidenceService` 每次 record/inspect 都重新读取：

- 当前 Rollback Outcome view，并要求 `outcome_authority=true`；
- Fresh Promotion Input 及其内嵌 Prior Input；
- Prior Input 指向的 Final Evaluation；
- Final Evaluation 中每个 lane 指向的当前 Harness H5c receipt。

只有 lineage、时间顺序、Candidate、平台覆盖和所有 comparison 全部一致时，
`before_after_authority=true`。删除或篡改任一 H5c 后，不会继续展示“已记录”。

## 双通道与产品展示

- Agent Tool：`evolution_proposal_before_after_evidence(request_id)`；
- 共享 Slash：`/evolution outcome-before-after <rollback-request-id>`；
- Workbench projection：保留 Proposal governance `approved`，Outcome 仍为 `rolled_back`，新增 exact
  `before_after_evidence`；
- New UI / TUI：显示 Evidence ID、lane 数和明确口径；长期指标继续显示“尚未记录”；
- 前端 strict protocol 拒绝 before/after bool 与 nested evidence 不一致、跨 Outcome/Proposal 绑定、lane 缺口、
  post-rollback/learning/promotion 越权。

该登记动作只写审计 evidence，不执行模型、代码、Git、发布或外部网络操作，因此不需要高风险二次确认；normal
与 bypass 都走同一 authority service。

## 验收证据

- 真实 HAR-08/H5c + Evolution Final Evaluation 场景产生 2 lanes RED/GREEN evidence；
- 独立 Harness DB 与 Evolution DB 的真实双库边界通过；
- 8 路并发重复 Evolution record 返回同一 immutable artifact；
- 同一 Outcome 的不同 `recorded_at` artifact 被 conflict gate 拒绝；
- H5c source 缺失时 record 返回 `proposal_before_after_comparison_stale`；
- Proposal projection 对失效 evidence fail-closed；
- Agent Tool 与共享 Slash 返回同一中文回执；
- Workbench、New UI、TUI 显示同一 Evidence ID 和 lane count；
- Python 定向测试、Node protocol/render 测试、ruff、py_compile 通过。

## 未完成边界

HAR-09 整体仍为 partial。后续应独立实现：

1. `HAR-09.6c Post-Rollback Verification Evidence`：证明版本槽回滚后的实际运行身份与恢复评测；
2. `HAR-09.6d Long-Term Outcome Window`：带窗口、样本覆盖与 censoring 的长期指标；
3. 成功 rollout 的 `promoted` Outcome 与 supersede ledger；
4. 只有长期 authority 成立后，才允许 EVO-06 消费结果进行 policy learning。

本切片不能被上述模块用作替代证据。
