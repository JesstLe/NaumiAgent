# NaumiAgent Mac Agent Workbench Test Strategy

> 本文定义 Workbench MVP 的测试分层、真实场景验收和发布准入。

## 1. 测试目标

Workbench 的测试不是只证明代码能 import，而是证明多 Agent 协作行为受控：

1. 任务认领不会冲突。
2. lease 过期能恢复。
3. worktree 不会误删用户工作。
4. 意图锁能阻止越界执行。
5. 验证失败能生成可操作 Failure Card。
6. Dashboard snapshot 能被 API 和 UI 正确消费。
7. 事件协议前后端一致。

## 2. 测试金字塔

```text
E2E / Scenario Replay
  ↑
API / Protocol Contract Tests
  ↑
Integration Tests
  ↑
Unit Tests
```

MVP 优先级：

1. Unit tests 覆盖状态机和策略。
2. Integration tests 覆盖 TaskStore + WorkbenchStore + WorktreeManager。
3. API/Protocol tests 防止前后端合同漂移。
4. E2E scenario replay 验证用户可见闭环。

## 3. 单元测试范围

### 3.1 Domain Models

文件：

```text
tests/unit/test_workbench_models.py
```

必须覆盖：

- 默认 risk、parallel mode、approval 策略。
- enum 值稳定。
- event payload JSON-ready。

### 3.2 WorkbenchStore

文件：

```text
tests/unit/test_workbench_store.py
```

必须覆盖：

- Mission 创建。
- Issue metadata round trip。
- Decision 写入和读取。
- IntentLock 写入和读取。
- AuditEvent append-only。
- FailureCard 查询。

### 3.3 Policy

文件：

```text
tests/unit/test_workbench_policy.py
```

必须覆盖：

- blocked path 命中后进入 Proposal Mode。
- high risk 命中阈值后进入 Proposal Mode。
- 非 active lock 不生效。
- 不同 mission 的 lock 不串扰。

### 3.4 TaskMarket

文件：

```text
tests/unit/test_workbench_market.py
```

必须覆盖：

- claim 创建 active lease。
- claim 更新 TaskStore owner/status。
- exclusive Issue 拒绝第二个 active claim。
- completed Task 不可 claim。
- lease expired 后释放任务。
- claim 记录 audit event。

### 3.5 Context Health

文件：

```text
tests/unit/test_workbench_context_health.py
```

必须覆盖：

- 缺 mission goal -> missing。
- 缺 acceptance criteria -> missing。
- 超过同步时间 -> stale。
- token load 过高 -> overloaded。
- policy conflict -> conflicted。

### 3.6 ValidationRunner

文件：

```text
tests/unit/test_workbench_validation.py
```

必须覆盖：

- allowlisted command 可执行。
- 非 allowlisted command 被拒绝。
- exit 0 记录 passed。
- exit 非 0 记录 failed。
- failed 生成 FailureCard。
- timeout 生成失败结果。

## 4. 集成测试范围

### 4.1 Worktree 集成

基于现有：

```text
tests/unit/test_worktree.py
```

新增覆盖：

- claim 后创建 worktree，IssueMetadata 写入 `related_worktree`。
- dirty worktree 不可删除。
- kept worktree 在 Dashboard 中可见。
- missing worktree 触发 warning 或 failure。

### 4.2 Engine 集成

文件：

```text
tests/unit/test_engine.py
```

必须覆盖：

- Engine 初始化 `workbench_store`。
- Engine 初始化 `workbench_service`。
- Tool registry 包含安全 workbench tools。
- Plan mode 或 lockdown 模式下只允许 read/proposal 类工具。

### 4.3 TaskStore 集成

基于：

```text
tests/unit/test_tasks.py
```

必须验证：

- Workbench 不复制 Task status。
- Task completed 后不可重新 claim。
- Task dependencies 未完成时不可进入 executable claim。

## 5. API 测试

文件：

```text
tests/unit/test_api_workbench.py
```

必须覆盖：

- 不存在 session 返回 404。
- snapshot 返回 `session_id`、`missions`、`tasks`、`issues`、`failures`、`events`。
- 写接口失败时返回中文错误。
- API 不暴露 secret。

最小 snapshot 响应：

```json
{
  "session_id": "sess_1",
  "missions": [],
  "tasks": [],
  "issues": [],
  "failures": [],
  "events": []
}
```

## 6. UI Protocol 测试

文件：

```text
frontend/terminal-ui/protocol-contract.json
frontend/terminal-ui/test/protocol.test.js
frontend/terminal-ui/test/state.test.js
frontend/terminal-ui/test/components.test.js
```

必须覆盖：

- `workbench/snapshot` 是合法 server event。
- `workbench/event` 是合法 server event。
- payload 被标准化。
- state 存储 snapshot。
- task panel 能显示 risk、parallel mode、worktree。

规则：

- 每次改 backend event 字段，都必须同步 `protocol-contract.json`。
- 每次改 `protocol-contract.json`，都必须跑 frontend tests。

## 7. E2E Scenario Replay

文件：

```text
tests/e2e/ui_scenarios/workbench_dashboard.yaml
tests/e2e/test_ui_scenarios.py
```

最小场景：

```text
workbench/snapshot
  -> Mission: Mac 工作台
  -> Task: 实现任务市场
  -> Issue risk: high
  -> Worktree: issue-1-backend
```

断言：

- UI 输出包含 Mission title。
- UI 输出包含 Agent/owner。
- UI 输出包含 risk。
- UI 输出包含 worktree。

## 8. 真实场景验收

在本地 NaumiAgent 仓库跑一条真实链路：

```text
Create Mission
  -> Create Task
  -> Attach IssueMetadata
  -> Claim by Backend-Agent
  -> Bind Worktree
  -> Run pytest target
  -> Record ValidationRun
  -> If failed, create FailureCard
  -> Read Dashboard snapshot
```

验收命令示例：

```bash
pytest tests/unit/test_workbench_market.py -q
pytest tests/unit/test_workbench_validation.py -q
pytest tests/unit/test_api_workbench.py -q
cd frontend/terminal-ui && npm test -- protocol.test.js state.test.js components.test.js
```

## 9. 回归测试矩阵

| 改动类型 | 必跑测试 |
|----------|----------|
| Domain model | `test_workbench_models.py`, `test_workbench_store.py` |
| Store schema | `test_workbench_store.py`, migration/backward compatibility test |
| Claim/Lease | `test_workbench_market.py`, `test_tasks.py` |
| Worktree | `test_worktree.py`, `test_workbench_market.py` |
| Validation | `test_workbench_validation.py` |
| API | `test_api_workbench.py`, `test_api.py` |
| UI protocol | frontend protocol/state/components tests |
| Dashboard rendering | E2E UI scenario replay |

## 10. 发布准入

MVP 合入前必须通过：

```bash
ruff check src/ tests/
pytest tests/unit/test_workbench_models.py \
  tests/unit/test_workbench_store.py \
  tests/unit/test_workbench_policy.py \
  tests/unit/test_workbench_market.py \
  tests/unit/test_workbench_context_health.py \
  tests/unit/test_workbench_validation.py \
  tests/unit/test_workbench_service.py \
  tests/unit/test_api_workbench.py \
  tests/unit/test_worktree.py \
  tests/unit/test_engine.py -q
cd frontend/terminal-ui && npm test -- protocol.test.js state.test.js components.test.js
pytest tests/e2e/test_ui_scenarios.py -q
```

## 11. 人工自审清单

每个功能提交前确认：

- 有真实状态读写，不是 prompt 套壳。
- 有失败路径测试。
- 有中文用户可见错误。
- 没有绕过 TaskStore 或 WorktreeManager。
- 高风险行为没有直接暴露给 LLM 工具。
- AuditEvent 不包含 secret。
- UI contract 和后端字段同步。

## 12. 后续仿真测试

MVP 后应增加 simulation runner：

```text
Scenario: 5 agents, 20 issues
Expected:
- no duplicate exclusive ownership
- no lease deadlock
- no circular dependency
- all completed issues have validation evidence
- all failures are visible as cards
```

这个仿真测试是未来多 Agent 真正自治前的关键门槛。
