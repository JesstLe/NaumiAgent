# EVO-06.1a Outcome-backed Opportunity Discovery

## 状态

Implemented。依赖 `EVO-05.7a` 的真实 `rolled_back` Outcome、`EVO-01` Candidate Store、
`HAR-09` 反馈/投影基础与现有 Evolution Tool/Slash/UI 通道。

## 用户价值

一次真实发布回滚不再停留在“已恢复”的终点。用户或 Agent 可以把该 Outcome 明确回注下一轮
改进发现，得到一个可审查、可聚合、不可执行的 Candidate；相同失败根因再次出现时汇入同一个
Candidate，而不是产生无法治理的建议文本。

## 权威边界

1. `EvolutionRevalidationRollbackOutcome` 是唯一来源事实；本模块不复制补丁、源码、工作区绝对路径、
   Workbench 自由文本或凭据。
2. `EvolutionCandidateStore` 是 Opportunity 的持久权威；不创建第二套 Opportunity 数据库。
3. `rollback_outcome` Evidence 只保存 content-addressed Outcome URI、完整 SHA-256、机械 finding、
   相对 scope 和时间。
4. Candidate 永远保持 `experiment_eligible=false`；发现动作不签发 Experiment Contract、Decision、
   promotion 或 learning authority。
5. Review 和 Workbench 入队前重新读取 Outcome 及全部 durable dependencies。任一来源缺失、摘要漂移、
   workspace 不匹配或 authority 失效，`source_authority` Gate 硬阻断 Proposal。

## 确定性映射

| 字段 | 规则 |
|---|---|
| Source | `evolution-outcome://rollback/<outcome-id>` + Outcome SHA-256 |
| Finding | `rollback_guardrail_breach` |
| Scope | `evolution:rollout:<proposal-kind>` |
| Root fingerprint | sorted breach reasons + finding + proposal kind + scope |
| Evidence ID | Outcome ID/SHA + root fingerprint 的 SHA-256 前缀 |
| Candidate ID | 复用 EVO-01 finding/root/scope 确定性 identity |
| Metric | `harness.rollback_guardrail_breach.rate decrease 0` |

同一个 Outcome 重放不会增加 evidence 或 revision；不同 Outcome 若 proposal kind 与 breach reasons 相同，
会增加同一 Candidate 的 occurrence/revision。不同根因不会被误聚合。

## 双通道与界面

- Agent Tool：`evolution_discover_outcome_opportunity(outcome_id=...)`
- Slash：`/evolution discover-outcome <rollback-outcome-id>`
- New UI/TUI：写入命令复用 Slash/ToolExecution；完成后使用
  `/evolution list --source rollback_outcome` 和 `/evolution detail <candidate-id>` 查看同一 Store 投影。
- 工具输出明确展示 Outcome、Candidate revision、Evidence 与“未授予实验或推广权限”。
- 权限：permissive/moderate/strict 为 `MEDIUM` 且无需确认，lockdown 阻断，bypass 按全权限语义直通；
  所有模式仍必须通过 Outcome/workspace/digest authority 门。

## 错误与并发

- 非法 ID、Outcome 不存在、损坏、失权或跨工作区均 fail closed，且不写 Candidate。
- Candidate Store 事务和唯一 Evidence identity 保证并发重复发现收敛到一条记录。
- Review 的动态来源复核异常按无 authority 处理，不将存储或 I/O 失败误报为可审查。
- 过滤器只接受登记过的 `rollback_outcome` source kind，不接受任意文本。

## 验收标准

- [x] 真实 rollback execution receipt 形成 Outcome 后可回注 Candidate。
- [x] 8 路并发回注同一 Outcome 只产生一个 Evidence、revision 1。
- [x] 两个同根 Outcome 聚合到同一 Candidate，occurrence/revision 增长。
- [x] Evidence 不包含 workspace 绝对路径、Proposal ID、slot ID、源码或补丁。
- [x] Outcome dependency 被篡改后，Review 失去 Proposal，重复 discover 被拒绝。
- [x] Agent Tool 与 Slash 共享同一 `discover()`；New UI/TUI 可按来源筛选同一结果。
- [x] 非法 ID、非 Outcome 类型与缺失来源有稳定错误路径。
- [x] Python/Node 协议、语法、Ruff 与相关模块测试通过。

## 未包含的后续模块

- EVO-06.1b：从 accepted/promoted Outcome、成本/延迟热点和明确缺失能力发现机会。
- EVO-06.1c：跨 Outcome 类型的时间窗聚类、优先级和预算排序。
- EVO-06.2：从 review-ready Opportunity 形成完整 Capability Proposal。
- EVO-06.3+：临时注册、shadow、limited activation、选择和退休。

这些能力不得通过本模块的 `rolled_back` 事实被提前宣称完成。
