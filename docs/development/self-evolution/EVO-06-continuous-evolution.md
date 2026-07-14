# EVO-06 持续学习与能力扩展

## 目标

在前五道门稳定后，允许 Agent 设计新 Tool/Skill/策略或架构候选，形成长期但受预算、证据和治理
约束的能力扩展循环。

## 子模块

- EVO-06.1 Opportunity discovery：失败聚类、缺失能力、成本/延迟热点、用户明确需求。
- EVO-06.2 Capability proposal：API、双通道、权限、数据、测试、维护者、淘汰标准。
- EVO-06.3 Sandbox registration：临时 registry/namespace，不能覆盖内置 tool。
- EVO-06.4 Shadow evaluation：观察建议调用但不执行，比较路由准确度和价值。
- EVO-06.5 Limited activation：低风险、明确 scope、预算和用户可见标识。
- EVO-06.6 Market/selection：性能、可靠、成本、用户价值，多指标而非 token 竞争。
- EVO-06.7 Retirement：低价值/高风险能力禁用、迁移、历史 replay 兼容。
- EVO-06.8 Meta-governance：进化规则自身只能通过更高级审批和固定基准修改。

## 验收标准

- 新 Tool 有真实代码逻辑、Tool schema、slash/Agent 双通道、权限与真实 E2E。
- shadow 阶段不产生副作用；limited activation 可即时禁用。
- 选择机制不奖励无限调用、隐藏失败、降低测试或消耗更多 token。
- 进化规则、Eval baseline、PermissionChecker、签名更新不能由普通进化循环自行改写。
- 每个能力有 owner、版本、兼容、SLO 和退休标准。
- A5：长期 soak、能力引入/禁用/回退、反馈闭环和预算审计。

## 终极边界

“自主进化”意味着自主提出并验证候选，不意味着绕过用户、权限、审核、签名和可回滚发布。
