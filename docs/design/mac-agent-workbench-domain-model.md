# NaumiAgent Mac Agent Workbench Domain Model

> 本文定义 Workbench 的核心实体、状态机和持久化边界。

## 1. 建模原则

1. Task 基础生命周期复用现有 `TaskStore`。
2. Workbench 只添加协作治理元数据。
3. 每个可见对象都必须能进入 audit log。
4. 每个执行型对象都必须有明确 owner、状态和失败路径。

## 2. 实体总览

```text
Mission
  ├── IssueMetadata -> TaskStore.Task
  │     ├── Lease
  │     ├── WorktreeRecord
  │     ├── ValidationRun
  │     ├── FailureCard
  │     └── Approval
  ├── IntentLock
  ├── Decision
  └── AuditEvent

AgentProfile
ContextSnapshot
```

## 3. Mission

Mission 表示用户当前的大目标。

字段：

```text
id
session_id
title
goal
status: planning | active | paused | completed | archived
created_at
updated_at
```

规则：

- 一个 session 可以有多个 mission。
- Dashboard 默认展示最近 active mission。
- Mission 关闭后，新的 claim 默认不可继续绑定到它。

## 4. IssueMetadata

IssueMetadata 是对现有 Task 的扩展。

字段：

```text
session_id
task_id
mission_id
parallel_mode: exclusive | cooperative | competitive | exploratory
risk_level: low | medium | high | critical
requires_human_approval
acceptance_criteria[]
expected_artifacts[]
related_branch
related_worktree
related_pr
created_at
updated_at
```

规则：

- `task_id` 必须指向 `TaskStore.tasks`。
- `acceptance_criteria` 不能为空时才允许进入可认领状态。
- `critical` 默认只能进入 Proposal Mode。
- `exclusive` 同时只允许一个 active lease。

## 5. AgentProfile

AgentProfile 描述 Agent 的能力和权限。

字段：

```text
id
session_id
name
role: planner | worker | reviewer | tester | explorer | maintainer
capabilities[]
permissions[]
max_parallel_tasks
status: idle | bidding | working | reviewing | blocked | paused
created_at
updated_at
```

规则：

- `worker` 可以 claim。
- `reviewer` 可以创建 review result，但不能直接 approve high risk。
- `explorer` 只能创建 proposal。
- `maintainer` 可以处理 low risk auto-merge candidate，但 MVP 不启用自动合并。

## 6. Lease

Lease 表示 Agent 对 Issue 的临时占用权。

字段：

```text
id
session_id
task_id
agent_id
state: active | released | expired
expires_at
worktree_name
created_at
updated_at
```

状态机：

```text
active
  -> released
  -> expired
```

规则：

- active lease 只能有一个，除非 Issue 是 cooperative/competitive。
- lease 到期不删除 worktree。
- lease expired 必须写 audit event。
- lease expired 后 Task 可回到 pending 或 blocked。

## 7. IntentLock

IntentLock 表示人类当前意图约束。

字段：

```text
id
session_id
mission_id
rule
blocked_paths[]
allowed_paths[]
require_proposal_for_risk: low | medium | high | critical
active
created_at
```

例子：

```text
rule: 本轮不修改模型路由
blocked_paths: ["src/naumi_agent/model/"]
require_proposal_for_risk: high
```

规则：

- 命中 blocked path 只能创建 proposal。
- 风险等级大于等于阈值时只能创建 proposal。
- IntentLock 优先级高于 Agent 自评信心。

## 8. Decision

Decision 是长期约束或阶段性约束。

字段：

```text
id
session_id
mission_id
kind: principle | architecture | policy | temporary | experiment
title
content
actor
created_at
```

规则：

- `principle`、`architecture`、`policy` 是强约束。
- `temporary` 到期后可以归档。
- `experiment` 必须有回滚或撤销路径。

## 9. ValidationRun

ValidationRun 表示一次真实验证。

字段：

```text
id
session_id
task_id
actor
command[]
cwd
status: passed | failed | timeout | skipped
exit_code
output
started_at
completed_at
```

规则：

- command 必须在 allowlist。
- output 必须截断保存，避免 UI 和存储膨胀。
- failed/timeout 必须生成 FailureCard。

## 10. FailureCard

FailureCard 是可操作失败对象。

字段：

```text
id
session_id
task_id
kind
title
detail
source_id
status: open | assigned | resolved | dismissed
created_at
```

kind：

```text
lease_expired
agent_timeout
test_failed
merge_conflict
review_rejected
scope_violation
budget_exceeded
context_stale
permission_denied
worktree_dirty
```

规则：

- open failure 必须在 Dashboard 中可见。
- resolved failure 必须关联解决事件或后续 validation。
- dismissed failure 必须记录 human reason。

## 11. Approval

Approval 表示人类或 reviewer 的治理结论。

字段：

```text
id
session_id
task_id
state: waiting | approved | rejected | not_required
risk_level
reason
actor
created_at
updated_at
```

规则：

- high/critical 默认 waiting。
- rejected 必须创建 follow-up action。
- approved 不等于自动 merge，只代表允许进入下一阶段。

## 12. AuditEvent

AuditEvent 是所有行为的事实账本。

字段：

```text
id
session_id
type
actor
subject_id
payload
timestamp
```

规则：

- 不存敏感 secret。
- payload 必须 JSON 可序列化。
- 事件不可修改，只能追加。

## 13. ContextSnapshot

ContextSnapshot 表示某 Agent 当前上下文健康度。

字段：

```text
id
session_id
agent_id
task_id
health: good | stale | overloaded | missing | conflicted
reasons[]
created_at
```

规则：

- missing/context conflicted 会阻止直接执行。
- stale 要求 Agent 先同步任务、决策、代码状态。
- overloaded 要求压缩或分层上下文。

## 14. SQLite 表建议

```text
workbench_missions
workbench_issues
workbench_agent_profiles
workbench_leases
workbench_intent_locks
workbench_decisions
workbench_approvals
workbench_validation_runs
workbench_failures
workbench_audit_events
workbench_context_snapshots
```

## 15. 不变量

1. `TaskStore` 是 Task 状态唯一来源。
2. active lease 不允许悬挂到不存在的 Task。
3. FailureCard 必须关联 source。
4. high/critical 风险必须能追溯审批状态。
5. 每个写操作都必须产生 AuditEvent。
