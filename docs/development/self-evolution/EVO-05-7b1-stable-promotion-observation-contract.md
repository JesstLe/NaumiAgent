# EVO-05.7b1 Stable Promotion Observation Contract

## 目标

在 Population Finalization 已形成真实、当前有效的 stable runtime authority 后，冻结成功推广长期观察所需的第一份
不可变 policy artifact。该契约回答“哪一次完整稳定发布、由哪次审批和 Proposal 产生、从何时开始、按什么规则观察”，
但不读取发布后的 heartbeat，不计算长期指标，也不签发 `promoted` Outcome。

## 精确证据链

`EvolutionStablePromotionObservationContract` 必须从一个
`EvolutionStableRemotePopulationFinalizationReceipt` 出发，逐级动态重验：

1. exact current Population Finalization Receipt 与 current signed Population Snapshot；
2. exact Stable Population Completion Receipt 及其完整 member source-set；
3. Completion 绑定的 immutable Rollout Plan；
4. Plan 绑定的 current approved Fresh Decision 与 source-set；
5. Fresh Promotion Input 与 prior Promotion Input；
6. prior Input 绑定的 immutable Experiment Contract Authority；
7. Experiment Contract 中的 Workbench session、Proposal、Candidate revision 与 digest。

任一 ID、SHA、candidate、snapshot、plan、approval、input 或 Proposal binding 不一致均失败关闭。Population Finalization
与 Completion 必须同时保持动态 authority；仅有 historical completion fact 不能建立新的观察契约。

## v1 观察规则

- runtime surface：`new_ui`、`tui`；
- heartbeat chain origin：真实 `startup`，origin sequence 为 1；
- operational phase：`running`、`waiting`；
- breach phase：`failed`；
- censor phase：`draining`、`stopped`；
- 最短持续时间：3600 秒；
- 最少 operational sample：12；
- 最大样本数：5000；
- 最大 gap 与最新样本年龄：不得超过每个 sample 冻结的 `timeout_seconds`；
- runtime binding、sequence 和 previous hash 必须保持 exact、连续；
- `window_not_before_at` 固定为 Population Finalization 的 `finalized_at`，发布前 stable-stage 样本不得回填为长期效果。

## 持久化、并发和安全

- Store 与 Finalization、Completion、Plan、Promotion Input、Experiment Contract 共用 session SQLite；
- `BEGIN IMMEDIATE` 内复验五类 exact durable dependency；
- 同一 Population Finalization/Completion 只能绑定一个 content-addressed Contract；Plan 与 Experiment 可随新 Population
  Finalization 产生后续契约，不以错误的全局唯一约束阻断合法扩容；
- 独立 Service 并发写入必须收敛到同一行，lineage 变化必须 conflict；
- ID 使用固定前缀和 24 位小写十六进制；SQL 全部参数化；
- durable row 与 JSON identity 不一致时失败关闭；
- 不保存 secret、自由文本、源码或远端 payload。

## 双通道

- Agent Tool：`evolution_stable_promotion_observation_contract`；
- Slash：`/evolution stable-promotion-observation-contract <population-finalization-receipt-id>`；
- CLI、New UI 和 Textual TUI 继续使用同一 Slash/Tool/Service；
- 仅写治理 artifact，不执行进程、Git、网络或远端安装，Moderate/Bypass 均不二次确认。

## 验收标准

- [x] exact Finalization → Completion → Plan → Approval/Input → Experiment/Proposal lineage；
- [x] active stable runtime、approval、Proposal binding 分项动态投影；
- [x] contract ID/SHA 覆盖完整 canonical payload；
- [x] policy、未知字段、非 canonical workspace 与越权字段篡改失败关闭；
- [x] SQLite writer fence、并发幂等和 row identity 校验；
- [x] Agent Tool、Slash、权限规则和 Engine composition；
- [x] 中文回执明确展示四类前置和全部 authority 边界；
- [x] 新增小模块 pytest、ruff、compile、import 通过；未运行全量测试。

## Authority 边界与下一步

本切片只允许 `observation_contract_recorded=true`，固定保持：

- `long_term_metrics_recorded=false`；
- `observation_window_authority=false`；
- `promoted_outcome_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`。

下一独立切片 `EVO-05.7b2 Stable Promotion Runtime Observation Admission` 应把本契约逐字段绑定到 exact managed
New UI/TUI release identity 与 Finalization 后的 startup-origin Harness ledger。随后 7b3 才能聚合长期窗口并给出
sustained-health verdict；7b4 才能在独立审批策略下写入 `promoted` Outcome 与 supersede ledger。
