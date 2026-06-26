# NaumiAgent Mac Agent Workbench Architecture

> 本文定义 Mac Agent Workbench 的系统架构。它是 `docs/product/mac-agent-workbench-prd.md` 的技术落地版本。

## 1. 架构原则

1. **Local-first**  
   第一版只管理本机 workspace、本地 SQLite、本地 worktree、本地验证命令。

2. **复用现有内核**  
   Task 状态继续由 `TaskStore` 负责，worktree 生命周期继续由 `WorktreeManager` 负责，事件输出复用现有 streaming / terminal-ui 协议。

3. **Workbench 是治理层，不是 Agent 替代层**  
   Workbench 不直接实现 LLM 推理，也不替代 `AgentEngine`。它负责把 Agent 行为变成可治理的实体、事件和快照。

4. **可审计优先于自动化**  
   自动执行之前，必须先有 Mission、Issue、Lease、Validation、Decision、Audit Event。

## 2. 总体架构

```text
Mac App / terminal-ui
  │
  │ REST / WebSocket / JSONL bridge
  ▼
Workbench API Routes
  │
  ▼
WorkbenchService
  ├── TaskStore                 # existing: task lifecycle
  ├── WorkbenchStore            # new: mission, issue metadata, lease, audit
  ├── TaskMarket                # new: claim / lease / expiry
  ├── WorktreeManager           # existing: isolated git worktrees
  ├── ValidationRunner          # new: allowlisted verification runs
  ├── PolicyEvaluator           # new: intent lock and risk policy
  └── ContextHealthEvaluator    # new: stale / overloaded / missing / conflicted
```

## 3. 运行时边界

### 3.1 AgentEngine

`AgentEngine` 仍是 ReAct 主循环和工具注册中心。

Workbench 只在 Engine 中挂载：

```text
engine.task_store
engine.worktree_manager
engine.workbench_store
engine.workbench_service
```

Engine 可以注册安全的 workbench tools：

```text
workbench_snapshot
workbench_propose_issue
```

第一版不让 LLM 直接通过工具执行高风险治理操作，例如 approve、merge、delete worktree。

### 3.2 WorkbenchService

WorkbenchService 是 API、工具、UI bridge 共同使用的应用服务。

职责：

- 创建 Mission。
- 绑定 Issue metadata 到 Task。
- 生成 Dashboard snapshot。
- 通过 TaskMarket 执行 claim/release/expire。
- 通过 ValidationRunner 记录验证结果。
- 写入 audit event。

它不负责：

- 直接调用模型。
- 直接修改文件。
- 直接合并远程 PR。
- 绕过权限层执行命令。

### 3.3 WorkbenchStore

WorkbenchStore 使用 SQLite，和现有 session/task 数据同源或同目录。

它存储：

```text
Mission
IssueMetadata
AgentProfile
Lease
IntentLock
Decision
Approval
ValidationRun
FailureCard
AuditEvent
ContextSnapshot
```

它不复制 `Task.status`。Task 基础状态仍然来自 `TaskStore`。

### 3.4 TaskMarket

TaskMarket 管理“受约束的自由分工”。

核心规则：

- exclusive Issue 同时只能有一个 active lease。
- completed Task 不可认领。
- 命中 Intent Lock 的任务不可直接认领执行。
- lease 过期后任务回到可处理状态。
- claim/release/expire 必须记录 audit event。

### 3.5 ValidationRunner

ValidationRunner 管理“合并许可证”。

核心规则：

- 只能执行 allowlisted command。
- 每次执行生成 ValidationRun。
- 非零退出码生成 FailureCard。
- 输出必须截断并保留足够诊断信息。

## 4. 数据流

### 4.1 Mission 创建

```text
Human input
  -> Workbench API
  -> WorkbenchService.create_mission()
  -> WorkbenchStore.workbench_missions
  -> AuditEvent: mission.created
  -> Dashboard snapshot
```

### 4.2 Issue 认领

```text
Agent claim request
  -> TaskMarket.claim()
  -> TaskStore.get_task()
  -> WorkbenchStore.get_active_lease()
  -> PolicyEvaluator
  -> WorkbenchStore.create_lease()
  -> TaskStore.update_task(in_progress, owner)
  -> AuditEvent: issue.claimed
```

### 4.3 Worktree 绑定

```text
Claimed Issue
  -> WorktreeManager.create(name, task_id)
  -> Git worktree + branch
  -> Worktree metadata
  -> WorkbenchStore.set_issue_worktree()
  -> AuditEvent: worktree.created
```

### 4.4 验证失败

```text
Validation command
  -> ValidationRunner._ensure_allowed()
  -> subprocess
  -> WorkbenchStore.record_validation_run()
  -> WorkbenchStore.create_failure()
  -> AuditEvent: validation.failed
  -> Dashboard snapshot
```

## 5. API 边界

MVP REST API：

```text
GET  /api/v1/workbench/sessions/{session_id}/snapshot
POST /api/v1/workbench/sessions/{session_id}/missions
POST /api/v1/workbench/sessions/{session_id}/issues/{task_id}/claim
POST /api/v1/workbench/sessions/{session_id}/leases/{lease_id}/release
POST /api/v1/workbench/sessions/{session_id}/leases/expire
POST /api/v1/workbench/sessions/{session_id}/validations
POST /api/v1/workbench/sessions/{session_id}/intent-locks
POST /api/v1/workbench/sessions/{session_id}/decisions
```

MVP 可以先实现 snapshot，再逐步加写接口。

## 6. UI 边界

Mac App 和 terminal-ui 共享同一份 workbench event contract。

核心事件：

```text
workbench/snapshot
workbench/event
```

UI 不直接推断状态，必须以后端 snapshot 为准。

## 7. 与现有模块的关系

| 现有模块 | Workbench 使用方式 |
|----------|--------------------|
| `tasks.store.TaskStore` | 任务基础状态和依赖关系 |
| `worktree.manager.WorktreeManager` | worktree 创建、保留、删除、状态刷新 |
| `streaming.events` | 增加 workbench snapshot/event 类型 |
| `api.routes` | 增加 workbench route |
| `frontend/terminal-ui` | 增加协议字段和状态渲染 |
| `safety.permissions` | 后续接入 approve/delete/merge 等高风险动作 |

## 8. 增量实现策略

推荐顺序：

1. Domain models。
2. WorkbenchStore。
3. IntentLock / Policy。
4. TaskMarket lease。
5. Worktree binding。
6. ValidationRunner / FailureCard。
7. WorkbenchService snapshot。
8. API route。
9. terminal-ui protocol。
10. Dashboard rendering。

这和 `docs/superpowers/plans/2026-06-27-mac-agent-workbench-mvp.md` 保持一致。

## 9. 架构风险

| 风险 | 缓解 |
|------|------|
| Workbench 变成第二套任务系统 | Task 基础状态只存在 TaskStore，Workbench 只存 metadata |
| UI 协议漂移 | 更新 `protocol-contract.json` 并跑 frontend tests |
| Agent 绕过治理 | 高风险写接口不注册为 LLM 工具 |
| worktree 泄漏 | Dashboard 显示 dirty/kept/missing，清理必须经过状态检查 |
| 自动化过早 | MVP 只做到 merge candidate，不做自治合并 |
