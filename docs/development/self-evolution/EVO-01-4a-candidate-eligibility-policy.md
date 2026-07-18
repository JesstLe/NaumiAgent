# EVO-01.4a Candidate Eligibility Policy v2

## 目标

用纯函数、版本化规则判断 Candidate 是否具备进入人工审阅的证据条件，并明确列出阻断原因。
本切片同时实现 HAR-09.3a 的最小 Candidate policy，但不授予实验权限、不修改 Candidate Store，
也不把“用户反馈存在”错误解释为“缺陷已被机械证明”。

## 三种结论

- `review_ready`：证据足以进入人工审阅，但 `experiment_eligible` 仍固定为 `false`。
- `needs_evidence`：没有受保护范围或 verifier 问题，但证据强度不足。
- `blocked`：命中受保护 scope，或缺少受支持的机械 verifier。

## Evidence Strength v1

- 单条 Harness failure 或 Self-Review static finding 属于机械证据，可进入人工审阅。
- 直接用户反馈至少需要两个唯一 Evidence；单次报告只能保持 `needs_evidence`。
- 只有 Agent interpretation 时，无论重复多少次都不能升级；必须补充直接反馈或机械证据。
- feedback recurrence 只证明问题被重复报告，不证明修复正确；实验仍需后续 Harness/Eval contract。

## 受保护范围

v1 明确保护安全/权限、凭据、迁移与更新相关 authority scope，包括当前源码中的：

- `src/naumi_agent/safety/`
- `src/naumi_agent/config/credentials*`
- `src/naumi_agent/persistence/migrations*`
- `src/naumi_agent/update/`
- `safety:`、`permissions:`、`secret_storage:`、`migrations:`、`updater:` 逻辑 scope

命中后仍可查看 Candidate，但必须人工治理，不能自动实验；bypass 不改变该结论。

## 五个可解释 Gate

1. `protected_scope`
2. `evidence_strength`
3. `mechanical_verifier`
4. `cooldown_gate`
5. `experiment_contract`

`cooldown_gate` 在运行时由 Review Service 注入 HAR-09.5b1 的只读治理上下文：无生效冷却、冷却
到期或显著新证据时通过；活跃冷却、缺失可信截止时间或未知结论时 fail-closed。纯函数调用未绑定
上下文时仍不伪造通过。`experiment_contract` 继续固定未通过，因此 `review_ready` 只表示可进入
Review Queue，不等于允许修改代码。
每个 Gate 另有 `hard_block`；只有 protected scope 与 verifier 缺失属于不可继续的硬阻断，证据
不足、冷却记录和 experiment contract 缺失仍可通过后续证据或治理步骤补齐。

## 接入与验收

- `/evolution detail` 和 `evolution_candidates` Tool 通过现有 Review Service 展示同一 Assessment。
- 每个判断包含 `candidate-eligibility-v2`、稳定 reason code、通过状态和中文解释；v2 可接收只读
  Workbench 治理上下文，未绑定上下文时 fail-closed 且不伪造 cooldown 通过。
- 重复直接反馈、单次反馈、Agent-only、单条机械证据、受保护源码 scope 均有 focused tests。
- Assessment 不读取时钟、不访问网络、不写 Store；治理时间由 Workbench 在外层评估后作为不可变
  Context 注入，因此相同 Candidate + Context 必须得到相同结果。
- secret 不进入 Assessment 或 renderer。

## 后续

- HAR-09.2 补齐时间窗趋势与 provider/model/platform 聚合视图。
- HAR-09.5b2a 已把治理状态注入 Review Assessment 与 typed UI payload；HAR-09.5b2b/UI-10.6
  继续实现 Proposal 决策交互页。
- EVO-02.1/02.6 提供隔离 experiment contract 与完整 protected-scope guard，届时才允许计算真正的
  `experiment_eligible`。
