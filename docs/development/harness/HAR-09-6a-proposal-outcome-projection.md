# HAR-09.6a Proposal Outcome Projection

## 状态

已实现。

## 目标

把 EVO-05.7a 的 Proposal-bound `rolled_back` Outcome 以只读方式投影回 Workbench，并同步 New UI 与
Textual TUI。该投影回答“这个已批准 Proposal 后来发生了什么”，但不篡改 Proposal 的原始治理决策。

## 双轴状态模型

Workbench 必须同时保留两条事实轴：

- 治理轴：Proposal 仍为 `approved`，保留 reviewer、decision note、decision time 和审计历史；
- 实施轴：动态显示 `rolled_back` Outcome、authority 状态、Rollback Receipt、Experiment Contract、
  Candidate revision 和 breach reasons。

因此本切片禁止把 Proposal 数据行直接改写成新的 `rolled_back` governance state。否则会丢失“谁批准了什么”
这一原始治理事实，也会破坏既有 Proposal 状态机。

## 权威读取与并发边界

- `EvolutionRevalidationRollbackOutcomeStore.list_by_session()` 使用参数化 SQL，最多读取 100 条；第 101 条
  触发明确失败，不允许静默截断后生成不完整投影。
- `EvolutionProposalOutcomeProjectionService` 对每条 Outcome 调用既有动态 `inspect()`，重新验证完整
  rollback/Contract/Proposal lineage。
- 同一 Proposal 出现两个未 supersede Outcome 时返回 `proposal_outcome_ambiguous`，不猜测哪个有效。
- Store、解析或动态验证不可用时，Workbench 返回脱敏 `proposal_outcome_unavailable`，不泄露数据库路径或
  内部异常。
- Projection 固定 `contract_issue_allowed=false`、`promoted=false`、`learning_authority=false` 和
  `promotion_authority=false`。

## 服务端执行边界

UI 隐藏按钮不是权限控制。`EvolutionExperimentContractIssuer` 绑定同一个 Outcome reader，并在读取 Proposal
之后、查询或创建 Contract 之前执行 fail-closed 检查：

- 已存在任何 Proposal-bound rollback Outcome：返回 `experiment_contract_outcome_terminal`；
- Outcome source 缺失、异常、超限或返回错误绑定：返回
  `experiment_contract_outcome_source_unavailable`；
- 只有当前 Proposal 没有 Outcome 且 source 可验证时，才允许既有 Contract 单飞逻辑继续。

该规则覆盖 Agent Tool、Slash、Bridge、New UI 和 TUI，无法通过直接发送前端事件绕过。

## New UI 与 TUI

- Workbench snapshot 为每个 Proposal 增加 typed `outcome`、`outcome_status`、`outcome_error` 和
  `contract_issue_allowed`。
- New UI 协议严格校验内外 session/proposal 绑定、content identity 格式和所有负 authority 字段；篡改为
  `promoted=true`、跨 Proposal 绑定或重新开放 Contract 均拒绝整个快照。
- 两端使用黄色显示 `rolled_back`，绿色显示有效 authority，红色显示证据失效/来源不可用。
- 详情继续显示 `approved` 治理状态，同时显示 Outcome/Receipt/Contract/breach；终态 Proposal 不再显示或响应
  `c` 签发动作。
- 这是自动 projection，不新增重复 Slash 或 Agent Tool；底层 Outcome 已由 EVO-05.7a 的 Tool/Slash 提供显式
  查询入口。

## 验收标准与证据

- 真实 Git baseline/candidate、真实 rollback execution 和 durable Outcome 可投影为 `rolled_back`；
- Workbench 持久 Proposal 在投影前后都保持 `approved`；
- issuer 对终态 Outcome 和不可用 source 均 fail closed，且错误信息不包含底层异常；
- New UI 协议拒绝跨 Proposal Outcome 和 authority 提权；
- New UI/TUI 在 80/120/200 列宽或 Textual 测试环境中展示相同终态，且不能再次发送签发动作；
- 仅运行相关 Python/Node 小模块测试、Ruff、编译、public export、YAML 和 diff 检查，不以全量测试冒充本切片证据。

## 未完成边界

HAR-09 整体仍为 partial。本切片没有实现：

1. HAR-08 Proposal before/after comparison；
2. rollback 后长期观察窗口和指标；
3. 成功 rollout 的 `promoted` Outcome；
4. Outcome supersede ledger；
5. 由长期结果驱动的 EVO-06 policy learning。

HAR-09.6b 已消费原 Promotion Input 中 proposal-bound Final Evaluation 与 HAR-08 H5c authority，形成
`implementation_before_after` durable evidence；它没有把历史实验结果冒充为回滚后评测，也没有开放 learning
或 promotion。详见 `HAR-09-6b-before-after-outcome-evidence.md`。
