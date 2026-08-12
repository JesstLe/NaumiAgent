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
- [EVO-06.1c3](EVO-06-1c3-durable-tool-catalog-miss-opportunity.md) Durable exact Tool Catalog
  miss（已实现）：把 `select:<tool-name>` 精确缺失保存为工作区隔离、目录摘要绑定的动态 Evidence；
  目标工具出现、目录变化或记录篡改时撤权，自然语言查询不落库。
- [EVO-06.1c4](EVO-06-1c4-cross-source-opportunity-prioritization.md) Cross-source Opportunity
  Portfolio（已实现）：在固定 30 天、最多 500 条权威快照中完成 authority/冷却 Gate、同 lane 同日去重、
  domain 聚类、影响计数与透明评分；Agent-only、调用/token 数和簇成员数量不能刷榜。
- [EVO-06.2a](EVO-06-2a-capability-proposal-contract.md) Capability Proposal Contract
  （已实现）：只从当前 authority/cooldown/Portfolio 均有效的 capability Candidate 形成结构化只读提案；
  exact Tool miss 只继承精确名称，Goal need 不反推私密目标，API/权限/数据/owner/SLO 未知项显式阻断
  Sandbox。
- [EVO-06.2b](EVO-06-2b-interaction-backed-capability-specification.md) Interaction-backed Capability
  Specification（已实现）：以五步 Harness 持久交互形成 Candidate revision 绑定、append-only、可恢复的
  结构化规格；每步前后重验 authority，完整规格仍不授予 Sandbox/Shadow/执行权。EVO-06.2c 独立校验与
  治理决策见下一项。
- [EVO-06.2c](EVO-06-2c-capability-specification-governance.md) Capability Specification Governance
  （已实现）：独立重放五条 Harness 人工答案，形成确定性 Assessment 与 first-terminal-wins 人工决策；
  历史批准随证据变化动态撤权，approved 只允许进入 Sandbox 实现设计，不授予注册、Shadow 或执行权。
- [EVO-06.3a](EVO-06-3a-sealed-capability-artifact-admission-preview.md) Sealed Capability Artifact
  与 Sandbox 准入预检（已实现）：封存 approved Specification 对应的真实 Python Tool 源码，执行
  AST/schema/顶层副作用/内置名冲突/临时 namespace 检查；源码或治理变化动态撤权，但不 import、注册或执行。
- [EVO-06.3b1](EVO-06-3b1-executable-scenario-binding.md) Interaction-backed executable scenario
  binding（已实现）：把 prose verification 转为参数 schema、result/error oracle 与 timeout 均可机械校验的
  Harness 人工 Binding；Artifact 或交互来源变化动态撤权，仍不 import、执行或注册。
- [ARC-01.3d1](../architecture/ARC-01-3d1-structured-tool-failure-contract.md) 已补齐 EVO-06.3b2 所需的最小
  结构化 Tool 失败契约：稳定 error code、用户安全 message 与 retryable 贯通 Engine、事件、UI 和 Worker RPC；
  旧 Tool/RPC 保持兼容，但该契约自身不授予 Sandbox 或 Registry authority。
- [EVO-06.3b2a](EVO-06-3b2a-content-addressed-sandbox-execution-request.md) Content-addressed Sandbox
  Execution Request（已实现）：封存 exact Git revision/tree、Artifact/Binding/permission digests、候选/driver/
  scenario overlays、argv、timeout 与 oracle digest；来源漂移动态撤权，仍不 materialize、执行或授权。
- EVO-06.3b2b Sandbox execution 与临时注册 lease：在 ARC-04/Harness Sandbox 中验证已绑定场景、结果/错误
  schema 和权限观察后，才能获得短期、可撤销 Registry lease；不能覆盖内置 Tool。
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

当前 `EVO-06.1a/1b/1c1/1c2/1c3/1c4/2a/2b/2c/3a/3b1/3b2a` 已分别关闭 rolled_back、stable promoted Outcome、H5c 定量回归、
durable Goal 明确需求与 exact Tool Catalog miss 的发现断点，但不代表 EVO-06 完成：自然语言缺失意图、
更细粒度可验证语义聚类、真实 Sandbox execution/Receipt/registration、
Sandbox/Shadow/Limited Activation、选择、退休和 Meta-governance 仍待实现。

## 终极边界

“自主进化”意味着自主提出并验证候选，不意味着绕过用户、权限、审核、签名和可回滚发布。
