# NaumiAgent Mac Agent Workbench Event Protocol

> 本文定义 Workbench 的事件名称、payload 约定和 UI 协议边界。

## 1. 协议目标

事件协议用于把多 Agent 研发行为转成可审计、可回放、可渲染的事实流。

它服务三类消费者：

1. WorkbenchStore：持久化 audit log。
2. API/WebSocket：向 Mac App 输出实时事件。
3. terminal-ui：在现有前端协议中展示 snapshot/event。

## 2. 事件命名

采用点分层：

```text
domain.action
```

例子：

```text
mission.created
issue.claimed
lease.expired
validation.failed
approval.requested
```

UI bridge 事件使用斜杠命名：

```text
workbench/snapshot
workbench/event
```

## 3. AuditEvent 基础结构

```json
{
  "id": "evt_abc123",
  "session_id": "sess_1",
  "type": "issue.claimed",
  "actor": "Backend-Agent",
  "subject_id": "3",
  "payload": {
    "lease_id": "lease_1",
    "expires_at": "2026-06-27T14:45:00"
  },
  "timestamp": "2026-06-27T14:00:00"
}
```

字段规则：

- `id`：事件唯一 ID。
- `session_id`：会话边界。
- `type`：事件类型。
- `actor`：Human、system、Agent name。
- `subject_id`：主要对象 ID。
- `payload`：JSON 对象，不允许包含 secret。
- `timestamp`：ISO 8601。

## 4. Snapshot 协议

`workbench/snapshot` 用于一次性刷新 Dashboard。

```json
{
  "type": "workbench/snapshot",
  "version": 1,
  "payload": {
    "session_id": "sess_1",
    "missions": [],
    "tasks": [],
    "issues": [],
    "failures": [],
    "events": []
  }
}
```

规则：

- UI 以 snapshot 为准，不从零散 event 猜完整状态。
- event 只做增量提示和 timeline 追加。
- snapshot payload 必须能被 `frontend/terminal-ui/src/protocol.js` 标准化。

## 5. Event Bridge 协议

`workbench/event` 用于实时追加。

```json
{
  "type": "workbench/event",
  "version": 1,
  "payload": {
    "id": "evt_abc123",
    "type": "validation.failed",
    "actor": "Test-Agent",
    "subject_id": "3",
    "payload": {
      "validation_run_id": "run_1",
      "failure_id": "fail_1"
    },
    "timestamp": "2026-06-27T14:10:00"
  }
}
```

## 6. Mission 事件

### `mission.created`

```json
{
  "title": "Mac Agent Workbench",
  "goal": "可视化治理本地多 Agent 研发流程"
}
```

### `mission.paused`

```json
{
  "reason": "等待人工调整范围"
}
```

### `mission.completed`

```json
{
  "summary": "MVP 协作内核已完成",
  "completed_issues": 8
}
```

## 7. Issue / Task 事件

### `issue.created`

```json
{
  "mission_id": "m1",
  "task_id": "3",
  "risk_level": "medium",
  "parallel_mode": "exclusive",
  "acceptance_criteria": ["claim 冲突必须被拒绝"]
}
```

### `issue.claimed`

```json
{
  "lease_id": "lease_1",
  "agent_id": "Backend-Agent",
  "expires_at": "2026-06-27T14:45:00"
}
```

### `issue.released`

```json
{
  "lease_id": "lease_1",
  "agent_id": "Backend-Agent",
  "reason": "主动释放"
}
```

### `issue.blocked`

```json
{
  "reason": "等待 Issue #2 合并",
  "blocked_by": ["2"]
}
```

## 8. Lease 事件

### `lease.expired`

```json
{
  "lease_id": "lease_1",
  "agent_id": "Backend-Agent",
  "task_id": "3",
  "worktree_name": "issue-3-market"
}
```

规则：

- lease expired 不自动删除 worktree。
- 必须生成 FailureCard 或 Dashboard warning。

## 9. Worktree 事件

### `worktree.created`

```json
{
  "name": "issue-3-market",
  "branch": "naumi/worktree-issue-3-market",
  "path": "/Users/lv/Workspace/NaumiAgent/data/worktrees/issue-3-market",
  "task_id": "3"
}
```

### `worktree.kept`

```json
{
  "name": "issue-3-market",
  "reason": "验证失败，等待人工审查"
}
```

### `worktree.removed`

```json
{
  "name": "issue-3-market",
  "discard_changes": false
}
```

## 10. Validation 事件

### `validation.started`

```json
{
  "validation_run_id": "run_1",
  "task_id": "3",
  "command": ["pytest", "tests/unit/test_workbench_market.py", "-q"]
}
```

### `validation.passed`

```json
{
  "validation_run_id": "run_1",
  "exit_code": 0,
  "duration_ms": 1320
}
```

### `validation.failed`

```json
{
  "validation_run_id": "run_1",
  "exit_code": 1,
  "failure_id": "fail_1",
  "output_preview": "FAILED test_claim_conflict"
}
```

## 11. Governance 事件

### `intent_lock.created`

```json
{
  "mission_id": "m1",
  "rule": "本轮不修改模型路由",
  "blocked_paths": ["src/naumi_agent/model/"],
  "require_proposal_for_risk": "high"
}
```

### `decision.created`

```json
{
  "kind": "architecture",
  "title": "任务认领必须使用 lease",
  "content": "避免 Agent 崩溃后任务永久占用。"
}
```

### `approval.requested`

```json
{
  "task_id": "3",
  "risk_level": "high",
  "reason": "修改任务市场状态机"
}
```

### `approval.resolved`

```json
{
  "approval_id": "appr_1",
  "state": "approved",
  "actor": "Human",
  "reason": "测试通过，风险可接受"
}
```

## 12. Failure 事件

### `failure.created`

```json
{
  "failure_id": "fail_1",
  "kind": "test_failed",
  "task_id": "3",
  "source_id": "run_1",
  "title": "验证命令失败"
}
```

### `failure.resolved`

```json
{
  "failure_id": "fail_1",
  "resolution": "修复并重新通过验证",
  "validation_run_id": "run_2"
}
```

## 13. 事件兼容性规则

1. 新增字段必须向后兼容。
2. 不删除已有字段；废弃字段先标记 deprecated。
3. `protocol-contract.json` 变更必须更新 JS 测试。
4. Python event type 变更必须更新 API/streaming 测试。
5. payload 中不允许出现 API key、token、private key。

## 14. 最小 MVP 事件集

第一版必须实现：

```text
mission.created
issue.created
issue.claimed
lease.expired
worktree.created
validation.passed
validation.failed
failure.created
decision.created
intent_lock.created
workbench/snapshot
workbench/event
```
