# NaumiAgent Mac Agent Workbench Acceptance Criteria

> 本文定义 MVP 完成的可验收标准。它用于 PR review、阶段验收和人工自审。

## 1. MVP 完成定义

MVP 完成不等于 Mac App 已打包发布。MVP 完成指：

> 本地 Workbench 协作内核已经能围绕一个真实 NaumiAgent session 管理 Mission、Issue、Agent lease、worktree、validation、failure 和 dashboard snapshot。

## 2. 必须完成的用户价值

用户可以：

1. 创建一个 Mission。
2. 看到 Mission 下的任务和 Issue。
3. 看到哪个 Agent 认领了哪个 Issue。
4. 看到租约状态和过期时间。
5. 看到相关 worktree。
6. 看到验证结果。
7. 看到失败卡片和建议动作。
8. 通过 Dashboard snapshot 理解当前系统状态。

## 3. 功能验收

### 3.1 Mission

- 可以创建 Mission。
- Mission 出现在 snapshot。
- 创建行为进入 audit log。

### 3.2 Issue

- Issue metadata 可以绑定到现有 Task。
- Issue 包含 risk、parallel mode、acceptance criteria。
- Issue 出现在 snapshot。

### 3.3 Claim / Lease

- Agent 可以认领 pending Task。
- claim 后 TaskStore 状态变为 `in_progress`。
- claim 后 owner 变为 `agent:<name>`。
- exclusive Issue 拒绝第二个 active claim。
- completed Task 不能 claim。
- lease expired 后任务释放。

### 3.4 Worktree

- claim 可以关联 worktree。
- worktree 名称写入 IssueMetadata。
- dirty worktree 不允许默认删除。
- kept/missing 状态可见。

### 3.5 Intent Lock

- 可以创建 IntentLock。
- blocked path 命中后返回 proposal required。
- high risk 命中阈值后返回 proposal required。

### 3.6 Validation

- allowlisted command 可以运行。
- 非 allowlisted command 被拒绝。
- passed validation 被记录。
- failed validation 生成 FailureCard。

### 3.7 Dashboard

snapshot 至少包含：

```text
session_id
missions
tasks
issues
failures
events
```

### 3.8 UI Contract

- `workbench/snapshot` 可被 terminal-ui protocol 识别。
- `workbench/event` 可被 terminal-ui protocol 识别。
- task panel 可展示 risk 和 worktree。

### 3.9 真实数据边界（Real-Mode Fixture Boundary）

真实模式（非 `--preview-fixture`）下，Mac Workbench 不得混入任何 fixture/design 数据：

- Task Market 不得出现 `design-*` 行、`fixture-lease-*` 租约或 fabricated bids。
- Reviews 不得出现 fixture 文件变更、diff 行、timeline 或 agent notes。
- 空后端数据必须展示明确的空状态文案（如"暂无待审批请求"），而非用假数据填充。
- 预览模式（`--preview-fixture`）下可保留完整参考截图，并在顶部显示调试徽标。
- `TaskMarketDesignPresentation` / `ReviewsDesignPresentation` 的默认策略必须为 `.real`，忘记传 policy 时不得意外渲染 fixture。

验证：

```bash
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter "DesignPresentation"
```

### 3.10 本地闭环冒烟（M20）

一个真实本地流程必须为所有主要页面产生可见的真实数据：

```
创建 session
  -> 创建 mission
  -> 创建 issue
  -> claim lease
  -> 记录 context health
  -> 运行 validation（通过）
  -> 创建 + 审批 proposal（M16）
  -> 刷新 dashboard snapshot
  -> 列出 approvals / events / proposals
```

约束：

- snapshot 必须包含 mission、issue、lease、validation_run、proposal。
- 审计事件必须包含 `mission.created`、`issue.claimed`、`proposal.created`、`proposal.approved`。
- 非白名单校验命令被拒绝（中文错误）。
- 整个流程仅依赖 `127.0.0.1` 本地状态，不需要外网或 LLM API key。

验证：

```bash
pytest tests/e2e/test_mac_workbench_local_loop.py -q
```

## 4. 非功能验收

### 4.1 安全

- 高风险写操作不直接暴露给 LLM tool。
- ValidationRunner 只能执行 allowlisted command。
- AuditEvent 不记录 secret。
- dirty worktree 不被静默删除。

### 4.2 可观测

- 每个关键写操作都有 AuditEvent。
- FailureCard 有 source_id。
- ValidationRun 有 command、cwd、exit_code、output。

### 4.3 可维护

- Workbench 不复制 TaskStore 的 status。
- Workbench 模块边界清晰。
- API 和 UI contract 有测试。

## 5. 验收测试命令

```bash
ruff check src/ tests/
pytest tests/unit/test_workbench_models.py \
  tests/unit/test_workbench_store.py \
  tests/unit/test_workbench_policy.py \
  tests/unit/test_workbench_market.py \
  tests/unit/test_workbench_context_health.py \
  tests/unit/test_workbench_validation.py \
  tests/unit/test_workbench_service.py \
  tests/unit/test_workbench_export.py \
  tests/unit/test_api_workbench.py \
  tests/unit/test_worktree.py \
  tests/unit/test_engine.py -q
cd frontend/terminal-ui && npm test -- protocol.test.js state.test.js components.test.js
pytest tests/e2e/test_ui_scenarios.py -q
pytest tests/e2e/test_mac_workbench_local_loop.py -q
```

单一发布门（ruff + 后端 + 本地闭环冒烟 + Swift 测试 + 开发打包）：

```bash
apps/macos/NaumiAgentWorkbench/scripts/verify-dev-build.sh
```

## 6. 人工验收问题

合入前人工检查：

1. 用户打开 Dashboard 后能否知道系统现在在做什么？
2. 用户能否看出哪个 Agent 拥有什么任务？
3. 用户能否看出哪些任务失败了？
4. 用户能否知道下一步应该点什么或处理什么？
5. Agent 是否有机会绕过人类意图锁？
6. Agent 是否可能误删 worktree？
7. 测试失败是否只是日志，还是可操作卡片？

## 7. 不通过条件

出现以下任意情况，MVP 不可验收：

- claim 冲突没有被拒绝。
- lease 过期后任务永久占用。
- 失败验证不生成 FailureCard。
- snapshot 无法被 UI protocol 消费。
- 高风险操作能被 LLM tool 直接执行。
- Task status 在 TaskStore 和 WorkbenchStore 之间出现双写冲突。
- 文档和实际事件字段不一致。
- 真实模式下出现任何 `fixture-`、`design-` 前缀的假数据行或 fabricated bids/diff/review notes。
