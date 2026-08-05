# EVO-05.3d Revalidation Outcome

## 1. 目标

把 EVO-05.3c 的新 Harness validation evidence 收口为 durable Outcome，并在同一 SQLite 事务中把旧 promotion
authority 标记为 `superseded_for_promotion`。历史 artifact 保持 append-only、可审计，但不能被后续 rollout 误当成
current authority。

## 2. 原子失效账本

`evolution_promotion_authority_invalidations` 逐项绑定 authority kind/id/digest、Outcome identity、原因和失效时间。
Outcome 与全部失效项在 `BEGIN IMMEDIATE` 事务内一并提交；Validation Receipt 不存在、digest 不一致或并发输入冲突时
整体回滚。失效范围包括 Promotion Input 内的 Final Evaluation 等全部 evidence，以及 Promotion Input、Package、
Approval Requirement 和 Approval Decision。

幂等边界是 Validation Receipt，而不是 Request：Profile 升级使旧 Outcome stale 后，同一 Request 可以重新执行验证并
基于新 Receipt 追加新 Outcome；失效账本保留每次 supersession 链路，不覆盖历史。

新验证通过与失败都会失效旧 authority：失败证据更不能让旧审批继续晋级。失效不是删除历史数据，也不把新 Outcome
伪装成 promotion authority。

## 3. 动态 current 语义

首次签发前重新读取 Request、Validation Receipt、Promotion Input、当前 target head 及当前 Harness Profile/check plan。
任一来源漂移都会 fail closed。签发后 `inspect()` 仍重复相同检查；target 或 Profile 漂移会把 Outcome 动态投影为
`stale`，并关闭 rollout eligibility。

Outcome 状态：

- `validated`：本轮项目检查通过，可作为未来 staged rollout 的候选输入；
- `validation_failed`：新证据已形成但检查失败，禁止 rollout；
- View `stale`：签发后的 target/Profile/plan 已漂移，禁止 rollout。

无论通过或失败，都要求重新签发 Final Evaluation、重新聚合 Approval，并重新签署专业角色；`promotion_authority=false`。

## 4. 双通道

Agent Tool：`evolution_revalidation_outcome(request_id=...)`

手动入口：`/evolution revalidation-outcome <revalidation-request-id>`

两者调用同一 Service。该操作只写 append-only governance 数据，不执行 Git merge/push/publish，不需要高风险二次确认。

## 5. 验收标准

- passed Outcome 与旧 authority 失效记录原子提交且幂等；
- failed validation 同样失效旧 Final Evaluation/Approval 并阻止 rollout；
- Profile/check plan 漂移后已签发 Outcome 动态 stale；
- stale source 无法首次签发；
- Validation Receipt 本身不会被列入旧 authority；
- Tool 与 Slash 均可查询同一 durable Outcome；
- 仅运行本模块及相邻小模块测试，不运行全量测试。

## 6. 当前边界

本切片只形成 staged rollout 的安全前置，不重签 Final Evaluation/Approval，也不执行 rollout。
[EVO-05.3e](EVO-05-3e-fresh-evaluation-plan.md) 已先把 current `validated` Outcome 转为完整 fresh evaluation matrix；
后续必须执行该 Plan、重新形成 Final Evaluation 和专业审批后，EVO-05.4 才能开放。
