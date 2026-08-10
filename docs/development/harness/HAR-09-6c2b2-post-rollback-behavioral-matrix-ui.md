# HAR-09.6c2b2 Post-Rollback Behavioral Matrix UI Projection

## 状态

已实现。

## 目标

把 HAR-09.6c2b1 已签发、且当前仍具备动态 authority 的完整 Behavioral Matrix 投影到同一份
Workbench Reviews snapshot，并让 New UI 与 Textual TUI fallback 展示一致的总体 verdict、lane 来源和权限边界。

本切片不让前端查询 Matrix Store，也不让前端根据若干单 lane 猜测矩阵是否完整。唯一 producer 仍是
`EvolutionProposalOutcomeProjectionService`；`WorkbenchService`、Bridge、New UI protocol、New UI renderer 与
Textual TUI 依次消费并重验同一个 typed projection。

## 权威数据链

```text
Matrix Store + Matrix Service.inspect()
  -> EvolutionProposalOutcomeProjectionService
  -> EvolutionProposalOutcomeProjection
  -> WorkbenchService.dashboard_snapshot()
  -> Bridge workbench/snapshot
  -> New UI protocol + Reviews renderer
  -> Textual TUI Reviews formatter
```

### Projection Service

Projection Service 只在 Matrix Store 和 Matrix Service 同时绑定时启用该来源。每次投影必须：

1. 按 rolled-back Outcome ID 读取不可变 Matrix；
2. 调用 `EvolutionPostRollbackBehavioralMatrixService.inspect()` 动态重验 Coverage 和全部 local/remote lane；
3. 当 Matrix 缺失时保持向后兼容，投影 `null/false`；
4. 当 Matrix 存在但 authority stale 时让整个 Outcome projection 失败关闭，而不是静默隐藏失效证据；
5. 只在 Matrix、Before/After Evidence、Runtime Verification 三者 exact 绑定时设置
   `post_rollback_behavioral_evaluation_recorded=true`。

Projection 模型机械核对：

- Outcome ID/SHA；
- Before/After Evidence ID/SHA；
- Runtime Verification ID/SHA；
- Matrix 自身 `behavioral_evaluation_recorded=true`；
- `long_term_metrics_recorded=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

因此一个孤立 Matrix、错误 Proposal 的 Matrix，或已失效 remote signed result 都不能进入 Workbench。

### Workbench Service

`WorkbenchService` 不信任 reader 返回的任意 dict。它重新执行 Pydantic Matrix 模型校验，并重复核对 Outcome、
Before/After 和 Runtime Verification binding。任一 content identity、总体 verdict、lane 聚合或 authority 字段篡改，
都会把该 Proposal 的 Outcome 投影降级为 `proposal_outcome_unavailable`，且继续禁止 Contract 重签发。

## New UI 协议与渲染

New UI protocol 对 Matrix 使用 closed schema：

- 只接受 2 到 4 条连续排序 lane；
- 逐条校验 lane kind、platform、evidence kind/ID prefix、Comparison ID/SHA、evidence SHA 和 aware timestamp；
- 重新计算 local/remote count 和 fail-closed 总体 verdict；
- 拒绝重复 Comparison/evidence identity；
- 要求行为 authority 为 true，同时长期、learning、promotion authority 全部为 false；
- exact 绑定当前 Outcome、Before/After Evidence 与 Runtime Verification。

后端 Pydantic 模型负责 content-addressed SHA 的 canonical 重算；前端只接受后端已投影的 typed artifact，负责结构、
聚合和 cross-record binding 的第二道失败关闭，不复制 Python canonical JSON hashing。

Reviews 详情使用语义颜色：

| 内容 | 颜色 |
| --- | --- |
| `recovered` | 绿色 |
| `changed` / `incompatible` | 红色 |
| `inconclusive` | 黄色 |
| platform | 蓝色 |
| local installed-runtime evidence | 青色 |
| remote signed ingestion evidence | 品红色 |
| 长期 / Learning / Promotion 未授权 | 黄色 |

每条 lane 显示 order、platform、lane kind、recovery status 与 evidence source；窄终端继续通过统一 ANSI wrap 保证
每行不超过 viewport width。

## Textual TUI parity

Textual `/workbench` Reviews 读取同一 Workbench snapshot，不查询 Store，也不自行计算 verdict。详情显示：

- Matrix ID、lane count 与总体 recovery verdict；
- 最多四条 lane 的 order/kind/platform/status/evidence kind；
- Runtime Verification 只负责 mechanical recovery，Behavioral Matrix 由独立 authority 判定；
- 长期指标、Learning、Promotion 仍未授权。

Matrix 缺失时明确显示“尚未记录”；不会把 Runtime Verification 或单 lane 冒充为完整矩阵。

## 失败语义

| 失败 | 用户可见结果 | 权限结果 |
| --- | --- | --- |
| Matrix 不存在 | 行为矩阵尚未记录 | 不授予行为/学习/推广 authority |
| Store/Service 不可用 | Outcome source 暂不可用 | Contract 动作保持阻断 |
| Matrix stale | Outcome projection 失败关闭 | 不显示旧 authority |
| Matrix binding 篡改 | `proposal_outcome_unavailable` | 不接受前端对象 |
| 前端 lane/verdict 篡改 | protocol record 被拒绝 | 不进入 reducer/renderer |

## 验收标准与证据

1. Projection Service 将真实 typed Matrix 与 Before/After、Runtime Verification exact 绑定；
2. Matrix authority 动态变为 stale 后，Projection Service 返回稳定 stale code；
3. Workbench Service 接受合法 Matrix，并拒绝被篡改的总体 verdict；
4. New UI protocol 接受完整 local + remote Matrix，并重新计算总体 verdict；
5. New UI Reviews 在 80/120/200 列显示完整 Matrix 摘要且不越宽；
6. New UI 对总体 verdict、platform、local/remote evidence 和未授权边界使用不同语义颜色；
7. Textual TUI fallback 展示相同 Matrix ID、lane、verdict 和 authority 边界；
8. Engine composition root 将 6c2b1 Matrix Store/Service 注入 Outcome Projection Service；
9. Ruff、Python compile、相关 Python/Node 小模块测试与文档治理通过；
10. 不运行全量测试，不以无关测试结果冒充本切片验收。

对应测试：

- `tests/unit/test_workbench_service.py`；
- `tests/unit/test_tui_workbench_overview.py`；
- `tests/unit/test_post_rollback_behavioral_matrix.py`；
- `tests/unit/test_engine_port_bundle.py`；
- `frontend/terminal-ui/test/protocol.test.js`；
- `frontend/terminal-ui/test/render.test.js`。

## 当前不足与下一步

1. 当前主机仍没有真实 Windows/Linux daemon 的生产级执行证据；自动化 fixture 只能证明协议和 authority 链；
2. Matrix 没有 remote retry/supersede ledger，同一 lane 多 admission 仍失败关闭；
3. `HAR-09.6d1/6d2/6d3` 已完成长期观察契约、runtime admission 与四态动态窗口评估；
   6e Long-Term Outcome Authority 尚未实现；
4. `promoted` Outcome、Outcome supersede、配置/数据 rollback 和最终 policy learning 闭环仍未完成；
5. 后续 UI 应消费 6d 的独立长期 authority，不得扩充 Matrix 字段来冒充长期观察。
