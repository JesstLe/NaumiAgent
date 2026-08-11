# EVO-01.4b Composite Candidate Source Authority Router

## 状态

Implemented。它是 EVO-06.1c runtime metric/Eval Evidence adapter 的最小架构前置，并保持
EVO-06.1a rollback Outcome 与 EVO-06.1b promoted Outcome 的动态撤权语义。

## 问题

`EvolutionReviewService` 原先只有一个 `source_authority_reader` 插槽。EVO-06.1a/1b 尚未暴露冲突，
因为两种 Outcome Evidence 都由同一个 `EvolutionOutcomeOpportunityService` 校验；下一步加入 H5c Eval、
runtime metric 或其他动态来源时，第二次 `bind_source_authority_reader()` 会静默覆盖 Outcome reader，导致：

1. 已 supersede/stale 的 Outcome Candidate 可能绕过动态重验；
2. 新 Eval Candidate 与旧 Outcome Candidate 无法同时获得正确 gate；
3. composition root 是否漏接某类动态来源只能等运行时偶然发现。

## 权威注册表

`EVOLUTION_DYNAMIC_EVIDENCE_SOURCE_KINDS` 是动态 Evidence 的唯一代码注册表。当前包含：

- `eval_metric_regression`
- `goal_need`
- `rollback_outcome`
- `promoted_outcome`
- `tool_catalog_miss`

`EvolutionCandidateSourceAuthorityRouter` 构造时必须收到与注册表 **exact match** 的
`source_kind -> reader` 映射：

- 缺少任何 kind：启动失败；
- 多出未知 kind：启动失败；
- reader 没有 async `validate_candidate_sources()`：启动失败；
- source kind 不合法或注册数量超出 16：启动失败。

新增动态 Evidence 时必须同时更新注册表和 Engine 映射；只扩展 Evidence schema 而漏接 authority reader
会在 composition 阶段失败，不能降级成“默认有效”。EVO-06.1c1 已按此约束注册 H5c reader。

## 路由与并发

对一个 Candidate：

1. 从不可变 `candidate.source_kinds` 找到需要的 reader；
2. 同一个 reader 即使负责多个 source kind，也只调用一次；
3. 不同 reader 通过 `asyncio.gather()` 并发重验，避免来源数量线性增加 Review 延迟；
4. 每个 reader 必须返回精确布尔 `True`；`False`、字符串 truthy 值、I/O/Runtime/Type/Value 错误均失败关闭；
5. 只有静态 Evidence 的 Candidate 不调用动态 reader，保持现有 Self-Review/Harness/Feedback 路径；
6. router 不写 Store、不缓存 authority、不授予 experiment/promotion/execution/learning 权限。

映射复制后封装为只读 `MappingProxyType`，运行中不能替换 reader。Review 的 bind 方法也改为只允许
同一对象幂等重绑；第二个不同 reader 会明确报错，并提示在 composition root 使用组合器。

## Engine Composition

`AgentEngine` 在 Outcome Opportunity Service 构造完成后创建
`evolution_candidate_source_authority_router`，将 rollback/promoted 映射到 Outcome Service，并将
`eval_metric_regression` 映射到 H5c Opportunity Service，`goal_need` 映射到 Goal Need Opportunity
Service，`tool_catalog_miss` 映射到 Tool Catalog Miss Opportunity Service，再把完整 router 一次性绑定到
`EvolutionReviewService`。New UI、Textual TUI、Slash 和 Agent Tool
仍通过 Review Service 消费同一个 `source_authority` Gate，没有界面专属判断。

未绑定任何 reader 的 Review 保留 fail-closed fallback，并直接消费动态来源注册表；因此未来注册表扩展后，
即使独立测试或降级构造忘记注入 router，新动态 Evidence 也不会被误判为有效。

## 验收标准

- [x] exact registry 缺失、未知 source kind 和无效 reader 均在构造时失败。
- [x] 同一 reader 负责 rollback/promoted 时，对混合 Candidate 只调用一次。
- [x] 两个不同 reader 通过 barrier 测试证明并发进入，不是顺序执行。
- [x] `False`、非 bool truthy、I/O 与 Runtime 错误全部返回无 authority。
- [x] 纯静态 Candidate 不触发动态 reader。
- [x] Review reader 不可被第二个不同对象静默覆盖。
- [x] 真实 Engine composition 绑定 router，且 router 覆盖所有当前动态 source kind。
- [x] 既有 rollback/promoted Opportunity、动态撤权与 Review filter 聚焦回归通过。
- [x] Ruff、语法和相关小模块测试通过；不运行全量测试。

## 非目标与下一步

EVO-01.1c / EVO-06.1c1 已形成 `eval_metric_regression` Evidence；EVO-06.1c2 已形成 `goal_need`
Evidence；EVO-06.1c3 已形成 exact `tool_catalog_miss` Evidence。三者均接入独立 reader。后续实现
自然语言缺失意图、跨类型时间窗和 EVO-01.5 可解释 Prioritization。
