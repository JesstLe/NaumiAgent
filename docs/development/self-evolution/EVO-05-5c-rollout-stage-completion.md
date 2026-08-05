# EVO-05.5c Rollout Stage Completion Evidence

## 目标

把 EVO-05.5b 的 current passing Observation 冻结为可审计的 local-canary stage completion，作为后续
manual interaction / automatic advance authority 的唯一输入。本切片不进入 opt-in、不部署 candidate、不切换 version slot。

这一步解决 passing receipt 与 stage advance 之间的时间竞态：旧 passing 之后新增/丢失 terminal run、kill switch
变化或 Plan/Entry authority 漂移时，不能继续使用旧结果推进 rollout。

## 机械门

`EvolutionRevalidationRolloutStageCompletionService.complete()` 每次重新读取：

1. persisted Observation 必须属于 canonical workspace 且 status 为 `passing`；
2. Rollout Plan 必须仍由 current Fresh Decision 支撑，digest 与 Observation/Entry 完全一致；
3. transition 必须是计划中的 `local_canary → opt_in`；
4. crash-safe canary journal 的当前 terminal 集合和每个 event digest 必须与 Observation 完全相同；
5. completed run 和 observation window 均达到冻结阈值；
6. Stage Entry 之后不得出现 pause/resume control event，current control 必须保持 active；
7. SQLite `BEGIN IMMEDIATE` 再检查 Observation/Plan/Entry/Control dependency 后才持久化。

Completion 使用 content-addressed ID、按 Observation 幂等。并发调用只形成一条 durable artifact。

## 权限边界

- `stage_completed=true` 只证明 local-canary evidence 已冻结；
- high/critical 或 data-backup 计划投影为 `manual_interaction_required=true`；
- low/medium 可投影 `automatic_advance_eligible=true`，但仍固定 `next_stage_entry_authority=false`；
- deployment、rollback、promotion authority 全部固定 false；
- bypass 不能跳过 evidence、kill switch 或人工选择。

## 验收结果

- 真实 high-risk canary 完成计划要求的全部 run 后形成 manual completion；
- 8 个并发调用返回同一 completion；
- insufficient Observation 不能完成 stage；
- terminal evidence 删除和用户 pause 均 fail closed；
- production Engine 与公共 lazy exports 已接线；
- 相关 stage-completion/Engine 小模块 8 项通过，未运行全量测试。

## 下一切片

EVO-05.5d Stage Advance Authorization 必须消费本 Completion：

1. manual 路径创建 durable typed interaction，只有用户明确选择 advance 才签发；
2. automatic 路径仅对计划明确允许的 stage 开放；
3. authority 绑定 current kill-switch generation、Completion 和 exact next stage；
4. 仍不直接部署。后续 opt-in deployment receipt 才能把 candidate commit/build/slot 与 exposure cohort 绑定。
