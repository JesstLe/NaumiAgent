# EVO-05.5f5u Dynamic Stage Completion Inspection Port

## 目标

把 [EVO-05.5f5t](EVO-05-5f5t-current-population-trust-reconciliation.md) 的 current signed Population
对账与 [EVO-05.5f5r](EVO-05-5f5r-stable-completed-run-aggregation.md) 已有只读 `inspect()` 连接起来。
Stable Population Preview 不再只相信 durable receipt 中记录的历史状态，而会对每个 current member receipt 动态复核其
Plan、Baseline、Window、Outcome、ChatRun、usage、Binding 与 heartbeat source authority。

本切片只增加只读组合能力。它不会实例化或调用 5f5r 的 `assess()` 写路径，也不授予 stable rollout 或 promotion 执行权限。

## 组合边界

`EvolutionStableStageCompletionInspectionPort` 精确复用现有 5f5r Service 的只读签名：

```python
async def inspect(*, evidence_id: str, subject_id: str) \
        -> EvolutionRevalidationStableStageCompletionView
```

端口通过 `RuntimeServiceOverrides.stable_stage_completion_inspector` 注入，由 `RuntimeServices` 验证后传入
`EvolutionStablePopulationCandidatePreviewService`。这样部署组合可以复用一个已经存在的完整 5f5r Service，而预演模块无需
反向拥有二十多个可写 release/evolution services，也不会在读取候选时意外创建发布状态。

5f5u 交付时默认生产组合尚未构造 5f5r 图，未注入端口会显示 `missing` 并失败关闭。后续 5f5v 已用只读、source-lazy
factory 补齐默认端口，显式 Runtime Override 仍保持最高优先级。

## 动态重验协议

1. 对同一安装成员存在多条 receipt 时，只选择 `(assessed_at, evidence_id)` 最大的 current receipt；
2. 按稳定的 member ID 顺序调度，每批最多 16 个 `inspect()`，避免大 Population 无界并发；
3. 返回值必须是 exact `EvolutionRevalidationStableStageCompletionView`；
4. 返回的 `view.receipt` 必须与请求候选的完整 receipt 相等，不能只比较 ID；
5. 每个成员投影 `dynamically_revalidated`、`dynamic_stage_completion_authority` 与有界撤权原因；
6. OSError、RuntimeError、TypeError、ValueError 被转成规范化稳定原因码，不向 UI 泄露异常细节；
7. 缺端口、缺结果、身份不一致或检查异常均不计入动态 authoritative denominator。

只有以下条件全部成立时，`dynamic_revalidation_authority=true`：

- inspection port 已配置；
- Current Population Snapshot authority 为 true；
- durable candidate 完整且无成员/Intent/lineage 冲突；
- denominator 中每个成员均成功动态重验；
- 每个成员的 5f5r `stable_stage_completion_authority=true`；
- 没有动态 non-authoritative member。

该字段是只读候选 authority，不等于发布命令授权；`stable_rollout_authority` 与 `promotion_authority` 继续固定为 false。

## 用户与 Agent 通道

- Agent Tool：`evolution_stable_population_candidate_preview`；
- Slash：`/evolution stable-population-preview [population-snapshot-id] [limit]`；
- CLI、Textual TUI 与 New UI 复用同一 Tool、Slash Router 和 renderer；
- renderer 显示动态 authoritative/denominator、端口 configured/missing，并逐成员显示动态结果；
- 该能力只读且无需确认，bypass 不增加二次确认。

## 验收标准

- 两成员 passing receipts + current signed Population + exact 5f5r Views 得到 `dynamic_revalidation_authority=true`；
- 每个成员只动态重验 current receipt，重复 Intent 仍使 candidate fail-closed；
- 17 次检查的最大同时在途数为 16；
- 返回另一 receipt 的 View 产生 `dynamic_inspection_identity_mismatch`；
- 缺端口时成员显示 `dynamic_inspector_not_configured`，总体无 authority；
- 真实 5f5r insufficient Completion 经原 Service `inspect()` 后计为动态 non-authoritative；
- Runtime override 的对象身份原样进入 Engine 预演 Service，无效端口对象初始化失败；
- Tool 与 Slash 输出同一投影，仍不授予 stable rollout 或 promotion；
- 只运行本模块小测试、ruff、compile、public import 与 YAML 校验，不运行全量测试。

## 自我审视与下一步

本切片完成了“预演服务可以真实调用既有 5f5r 只读重验”的最小 ARC 组合边界。后续
[EVO-05.5f5v](EVO-05-5f5v-default-stable-read-graph.md) 已增加默认生产 source-lazy read graph：空候选不读取任何 source，首次有
候选时从现有 opt-in、HAR、ChatRun 与 release roots 恢复完整只读图。

即使所有成员动态 authoritative，本切片也没有签发 Population Completion Receipt、执行 expected-pointer CAS 或提供发布回滚
互锁；这些必须在后续独立模块中完成，不能把预演结果冒充真实 rollout。
