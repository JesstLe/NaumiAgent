# HAR-09.5b2a Eligibility Governance Context

## 目标

把 HAR-09.5b1 的持久 Proposal 治理事实接入 Candidate Review/Eligibility，使 CLI、TUI 与 New UI
看到的 cooldown Gate 与最终 enqueue 强制策略一致。本切片只提供只读状态，不在 Candidate 页面
新增 approve/reject/defer/merge 写操作。

## 唯一规则来源

- `proposal-governance-v1` 继续负责截止时间、显著新证据和 fail-closed 判定；
- `candidate-eligibility-v2` 只接收不可变 `CandidateGovernanceContext`，不自行读取数据库或时钟；
- Review Service 以同一时刻批量评估最多 500 个 Candidate，避免列表 N+1 查询；
- Evolution Queue Adapter 继续在写入前再次执行治理校验，UI 展示不能替代执行边界。

治理 Context 公开字段：policy version、allowed、稳定 reason、最近 Proposal state/revision、
cooldown 截止时间和 significant-new-evidence 标记。不得包含决策原文、绝对数据库路径或私密 Evidence。

## Gate 语义

| reason | cooldown Gate | Candidate 结论影响 |
|---|---|---|
| `no_active_cooldown` | 通过 | 由其他 Gate 决定 |
| `cooldown_expired` | 通过 | 可重新进入人工审阅 |
| `significant_new_evidence` | 通过 | 可生成新 revision Preview |
| `cooldown_active` | 未通过 | `review_ready` 降为 `needs_evidence` |
| `cooldown_record_missing` | 未通过 | fail-closed，等待人工复核 |
| 未绑定/未知 | 未通过 | 不伪造治理通过 |

protected scope 或 verifier 缺失仍优先产生 `blocked`。无论 cooldown 是否通过，
`experiment_eligible` 都固定为 false，approved 也不授予执行权限。

## 展示边界

- Markdown detail 增加 Workbench 治理区块；
- typed `evolution/review` detail 增加 `governance` 对象；
- 活跃冷却时 Proposal Preview 为 null，避免 UI 暗示可以入队；
- 未绑定治理 Reader 的独立纯函数/测试场景保持可用，但明确显示 Gate 未绑定；
- Candidate 页面继续标记 `read_only=true`。

## 验收标准

- 一次批量查询为每个 Candidate 选择最高 revision、最新更新时间的 Proposal；
- reject 后 detail 显示 `cooldown_active`、`needs_evidence`、无 Preview；
- occurrence 从 2 增至 3 仍阻断，从 2 增至 4 后显示 `significant_new_evidence` 并恢复 Preview；
- 旧记录缺少 cooldown 时不自动解除；
- typed payload 不暴露 decision note、Evidence 原文或存储路径；
- Queue 写入前仍独立复核 cooldown，不能通过伪造 UI payload 绕过；
- focused tests 覆盖纯策略、真实 SQLite、Review Service 和 typed UI 投影。

## 明确未包含

- Workbench Proposal 决策列表、键盘动作、确认表单（HAR-09.5b2b / UI-10.6）；
- approved 到 EVO-02 Experiment Contract 的转换；
- before/after outcome tracking。

下一切片应实现 Proposal 决策 UI，并直接调用 HAR-09.5b1 的现有 Service/API，不复制状态机。

## 后续实现状态（2026-07-18）

HAR-09.5b2b/UI-10.6a 已按上述边界实现 Proposal approve/reject/cancel，并复用 HAR-09.5b1 Service。
Candidate 页面仍然只读；决策入口位于 Workbench Reviews。尚未实现 defer/merge 表单、EVO-02 转换与
outcome tracking，详见 `../cli-ui/UI-10-6a-proposal-actions.md`。
