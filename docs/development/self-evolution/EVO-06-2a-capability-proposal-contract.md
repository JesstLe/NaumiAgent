# EVO-06.2a Capability Proposal Contract

## 目标

把 EVO-06.1c4 中当前可排序、来源 authority 有效且通过 Workbench cooldown Gate 的
`capability` Candidate 转换为一份结构化、可审查、不可注册、不可执行的 Capability Proposal。

本切片解决的是“进入能力设计阶段时，哪些事实已知、哪些仍未知、谁有权继续推进”。它不生成 Tool
代码，不猜测参数 schema，不注册临时能力，也不进入 Shadow 或 Limited Activation。EVO-06.2 尚未
整体完成；后续仍需受控的人机规格补全、Proposal 治理动作与签名快照。

## 与 HAR-09 Proposal Preview 的边界

HAR-09.4a `EvolutionProposalPreview` 描述已有知识、配置、Prompt、Tool、Test 或 Code 的改进方向，主要
回答“建议改哪一类内容、如何验证”。EVO-06.2 `EvolutionCapabilityProposal` 描述一项新能力从 API 到
退休的完整生命周期责任，必须额外回答：

- Tool 名、参数 schema、结果 schema、错误契约与版本策略；
- 权限 family 与 bypass 的 authority 边界；
- 输入、输出和 retention 数据分类；
- 真实 E2E、异常、极值、并发和 slash/Agent 双通道验证；
- owner、SLO、维护责任和退休条件；
- 是否具备 Sandbox、Shadow、Limited Activation 或执行资格。

二者可以同时出现在 Candidate detail 中，但 Capability Proposal 不能冒充 HAR-09 Workbench Proposal，
也不能继承其 `open/approved` 状态作为注册权。

## 来源与生成 Gate

生成器每次从同一 `EvolutionReviewService` 当前快照读取并机械检查：

1. Candidate Store revision 与完整 SHA-256 一致；
2. Candidate `kind=capability` 且 Eligibility 为 `review_ready`；
3. `source_authority` 与 `cooldown_gate` 当前通过；
4. Candidate 在当前 30 天 Opportunity Portfolio 中 `rankable=true`；
5. Priority Candidate ID、policy、domain、rank 与 Portfolio 一致；
6. scope 必须是受支持的脱敏 grammar：`capability:tool:<safe-name>` 或
   `capability:need:<objective-hash-prefix>`。

任一条件失败时不生成 Proposal。bypass 不改变这些 Gate。

## 已知信息与禁止猜测

### Exact Tool Catalog miss

`capability:tool:<safe-name>` 可提供精确 `requested_name`，但 Tool Search miss 不包含用户自然语言查询，
因此不能推断参数、返回值、权限和数据范围。这些字段保持 `unresolved`。

### Durable Goal need

Goal Candidate 只保存 objective 的短摘要指纹，不保存 objective、note、session 或用户原文。因此 Tool 名也
必须保持 `unresolved`，Proposal 不允许从哈希“重建”或由 LLM 猜测用户需求。

未知项不是空白字段，而是 `unresolved_requirements` 中的机械阻断项。当前至少包括 API schema、结果、
错误、版本、权限、数据、真实场景、owner、SLO 和维护责任；Goal need 还包括 Tool 名。

## Authority 不变量

- `status=needs_specification`、`requires_human_review=true`；
- 已授予 permission family 永远为空；
- bypass 不授予注册或执行权限；
- `sandbox_eligible=false`、`shadow_eligible=false`、
  `limited_activation_eligible=false`；
- `executable=false`、`registry_mutation_allowed=false`、
  `builtin_override_allowed=false`；
- next stage 只是 `sandbox_registration` 的治理方向，不是已获得资格；
- `proposal_id` 绑定 generator version、Candidate revision/digest、Portfolio anchor/rank/score、API 已知项与
  未决项；任一来源快照变化都会形成新身份。

Python Pydantic 与 Node protocol 会分别重算并验证该身份。Node 还会把 Candidate ID/revision/scope、
Priority rank/policy 与 typed page 当前快照交叉核对，拒绝传输层拼接旧 Proposal。

## 双通道与 UI

- 用户：`/evolution detail <candidate-id>`；
- Agent：`evolution_candidates(action='detail', candidate_id='<id>')`；
- New UI：同一 typed `evolution/review` detail 显示 Proposal ID、Portfolio 排名、Tool 名、未决分类和
  “可注册/可执行/Shadow 均为否”；
- Textual/legacy fallback：复用同一个 `render_evolution_review()` 文本结果。

所有入口调用同一生成器，读取不写 Candidate Store、Workbench、Registry 或 Git。

## 验证与验收证据

小模块测试覆盖：

- exact Tool miss 得到稳定 Tool 名和稳定 Proposal ID；
- Goal need 不泄露/猜测 objective 或 Tool 名；
- authority 撤销、Priority 不可排序、Candidate digest 篡改均 fail closed；
- schema 拒绝 `executable=true` 和 bypass authority 提升；
- 真实 `select:browser_trace_compare` → durable Miss → Candidate → authority 重验 → Portfolio →
  Capability Proposal E2E；
- Python 文本、typed payload、Node strict protocol 与 New UI renderer 内容一致；
- 80/120/200 列渲染保持边界。

## 当前不足与后续依赖

EVO-06.2a 刻意不把未知字段交给 LLM 自动填写。[EVO-06.2b](EVO-06-2b-interaction-backed-capability-specification.md)
现已通过 Harness typed interaction 让用户逐项提供 API、权限、数据、owner/SLO 和真实验收场景，并形成
带 revision 的不可变规格快照；它仍不得直接注册。下一最小切片是 EVO-06.2c 的独立校验与明确治理决策。
只有规格完整、校验通过且治理明确批准后，EVO-06.3 才能设计临时 namespace、
内置 Tool 冲突拒绝和可撤销 Sandbox Registry。
