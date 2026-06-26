# NaumiAgent Mac Agent Workbench User Flows

> 本文描述用户、Agent、系统服务之间的行为链路。它服务于产品设计、接口设计和 E2E 验收。

## 1. 角色

| 角色 | 职责 |
|------|------|
| Human | 创建 Mission、设置意图锁、审批风险、处理异常 |
| Planner-Agent | 拆分 Issue、建立依赖、提出验收标准 |
| Worker-Agent | 认领 Issue、绑定 worktree、实现代码或文档 |
| Reviewer-Agent | 审查变更、评估风险、提出修改意见 |
| Test-Agent | 运行验证、诊断失败、生成 Failure Card |
| Workbench System | 存储状态、执行策略、发布事件、生成 Dashboard |

## 2. Flow A: 创建 Mission 并拆分 Issue

### 触发

用户在 Dashboard 输入一个目标：

```text
设计并实现 NaumiAgent Mac Agent Workbench MVP。
```

### 系统动作

1. 创建 Mission。
2. 写入 audit event: `mission.created`。
3. Planner-Agent 生成 Issue 草案。
4. 每个 Issue 必须包含标题、目标、验收标准、风险等级、并行模式。
5. 写入 audit event: `issue.created`。

### 用户看到

```text
Mission: Mac Agent Workbench MVP
Status: planning
Issues: 8
Missing Acceptance: 0
High Risk: 2
```

### 验收

- 没有验收标准的 Issue 不允许进入可认领状态。
- Critical Issue 必须先进入 Proposal Mode。

## 3. Flow B: Agent 认领 Issue

### 触发

Worker-Agent 请求认领 Issue。

```text
claim issue #3 as Backend-Agent for 45 minutes
```

### 系统动作

1. 检查 Task 是否存在。
2. 检查 Task 是否已完成。
3. 检查是否有 active lease。
4. 检查依赖任务是否完成。
5. 检查 Intent Lock。
6. 创建 Lease。
7. 将 TaskStore 状态更新为 `in_progress`。
8. 记录 `issue.claimed`。

### 用户看到

```text
Issue #3 实现任务市场
Owner: Backend-Agent
Lease: active
Expires: 45 minutes
Worktree: not bound
```

### 异常

| 异常 | 行为 |
|------|------|
| 已有 active lease | 拒绝认领，返回当前 owner |
| 依赖未完成 | 标记 blocked |
| 命中意图锁 | 要求 proposal |
| 高风险任务 | 创建 approval request |

## 4. Flow C: 创建并绑定 Worktree

### 触发

Agent 或系统为 Issue 创建隔离工作区。

```text
worktree_create issue-3-market --task 3
```

### 系统动作

1. 调用现有 `WorktreeManager`。
2. 创建 Git branch。
3. 持久化 worktree record。
4. 回写 Task owner。
5. 回写 Issue `related_worktree`。
6. 记录 `worktree.created`。

### 用户看到

```text
Worktree: issue-3-market
Branch: naumi/worktree-issue-3-market
Status: clean
Task: #3
Removable: yes
```

### 验收

- dirty worktree 默认不能删除。
- kept worktree 必须显示保留原因。
- missing worktree 必须能从 Dashboard 识别。

## 5. Flow D: 运行验证并生成 Failure Card

### 触发

Agent 完成一个实现片段后运行验证。

```text
pytest tests/unit/test_workbench_market.py -q
```

### 系统动作

1. 检查命令是否在 allowlist。
2. 运行验证。
3. 记录 Validation Run。
4. 如果失败，创建 Failure Card。
5. 写入 `validation.passed` 或 `validation.failed`。

### 用户看到

成功：

```text
Validation Run #8
Status: passed
Command: pytest tests/unit/test_workbench_market.py -q
```

失败：

```text
Failure: test_failed
Affected Issue: #3
Source: Validation Run #8
Suggested: 指派 Backend-Agent 修复
```

## 6. Flow E: 人类审批

### 触发

Issue 完成，但风险等级要求人工审批。

### 系统动作

1. 收集 Issue、diff summary、validation runs、known risks。
2. 创建 Approval Card。
3. 等待 Human 操作。

### 用户操作

```text
Approve
Request Changes
Escalate Risk
Convert to Proposal
Keep Worktree
```

### 验收

- high 和 critical 风险不得自动通过。
- request changes 必须重新打开 Issue 或创建 follow-up Issue。
- approval/rejection 必须进入 audit log。

## 7. Flow F: Lease 过期

### 触发

Agent 长时间无进展，lease 到期。

### 系统动作

1. 标记 lease expired。
2. 将 Task 恢复为 pending 或 blocked。
3. 创建 Failure Card: `lease_expired`。
4. 保留 worktree，不自动删除。
5. 记录 `lease.expired`。

### 用户看到

```text
Issue #3 租约已过期
Previous Owner: Backend-Agent
Worktree: issue-3-market
Suggested:
- 续租
- 指派其他 Agent
- 保留审查
- 删除 clean worktree
```

## 8. Flow G: Proposal Mode

### 触发

任务命中意图锁、风险过高、或 Agent 不确定。

### 系统动作

1. 禁止直接执行代码修改。
2. 允许 Agent 创建 proposal。
3. proposal 进入 Next Step Pool 或待审批区。

### Proposal 内容

```text
目标
影响范围
拟修改文件
风险
验证计划
需要人类确认的问题
```

## 9. Flow H: Dashboard 回放

### 触发

用户选择 Mission 的 Timeline。

### 系统动作

按时间排序读取 Audit Events。

### 用户看到

```text
10:01 Human created mission
10:02 Planner-Agent created issue #1
10:05 Backend-Agent claimed issue #1
10:06 System created worktree issue-1-backend
10:15 Test-Agent ran validation #7
10:16 validation.failed -> Failure Card #2
10:22 Human requested changes
```

## 10. Flow 优先级

MVP 必须先跑通：

1. Mission -> Issue。
2. Issue -> Claim/Lease。
3. Lease -> Worktree。
4. Worktree -> Validation。
5. Validation Failed -> Failure Card。
6. Dashboard Snapshot -> UI Protocol。

审批、Proposal、回放可以先用最小可用形态实现，但数据模型必须预留。
