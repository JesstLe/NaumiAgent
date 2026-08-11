# EVO-06 持续学习与能力扩展

## 目标

在前五道门稳定后，允许 Agent 设计新 Tool/Skill/策略或架构候选，形成长期但受预算、证据和治理
约束的能力扩展循环。

## 子模块

- [EVO-06.1a](EVO-06-1a-outcome-backed-opportunity-discovery.md) Outcome-backed
  Opportunity discovery（已实现）：把动态有效的真实 `rolled_back` Outcome 确定性回注现有
  Candidate Store，并在审阅/入队前重新验证来源 authority。
- [EVO-06.1b](EVO-06-1b-promoted-outcome-opportunity-discovery.md) Promoted Outcome-backed
  Opportunity discovery（已实现）：把 current stable promoted Outcome 确定性回注为新的 Candidate，
  并在发现与 Review 前动态重验 head/supersession/Decision/Eligibility authority。
- [EVO-06.1c1](EVO-06-1c1-h5c-quantitative-regression-opportunity.md) H5c quantitative regression
  opportunity（已实现）：从完整重建的 H5a/H5c authority 发现经 95% CI 确认的主定量指标回归，形成
  动态可撤权的 latency/cost/token/general metric Candidate。
- [EVO-06.1c2](EVO-06-1c2-goal-backed-explicit-need-opportunity.md) Goal-backed explicit need
  opportunity（已实现）：只从用户显式创建且未终结的 durable Goal 形成脱敏 capability Candidate；
  完成/取消/篡改动态撤权，且在独立 Goal acceptance runner 出现前禁止自动实验。
- EVO-06.1c3+ Opportunity discovery（待实现）：durable Tool Search miss/缺失能力、跨类型时间窗聚类与可解释优先级。
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

## 真实闭环入口条件

EVO-06 不得从“LLM 生成了改进建议”直接开始。每次循环必须可追溯地消费 EVO-01..05 的真实 Outcome：候选源码与补丁、隔离执行、
RED/GREEN H5a、H5c comparison、失败归因、签名 Decision、staged rollout、运行监控、rollback/accept Outcome 缺一不可。Outcome 必须回注
opportunity discovery，并以新 Candidate ID 开启下一轮；不得原地改写上一轮证据或把未执行建议计为能力提升。

当前 `EVO-06.1a/1b/1c1/1c2` 已分别关闭 rolled_back、stable promoted Outcome、H5c 定量回归与
durable Goal 明确需求的发现断点，但不代表 EVO-06 完成：机械缺失能力、跨类型聚类与优先级、Capability Proposal、
Sandbox/Shadow/Limited Activation、选择、退休和 Meta-governance 仍待实现。

## 终极边界

“自主进化”意味着自主提出并验证候选，不意味着绕过用户、权限、审核、签名和可回滚发布。
