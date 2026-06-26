# NaumiAgent Mac Agent Workbench Governance

> 本文定义 Workbench 中人类治理、风险等级、权限边界和 Proposal Mode。

## 1. 治理目标

Workbench 的治理层要解决一个核心问题：

> 让 Agent 可以自主推进研发，但不能绕过用户当前意图、风险审批和可审计证据。

治理层不追求一开始完全自动化，而是先保证：

- 行动前有边界。
- 行动中有记录。
- 行动后有验证。
- 高风险有审批。
- 失败有恢复路径。

## 2. 权限角色

| 角色 | 可做 | 不可做 |
|------|------|--------|
| Observer | 读取 Dashboard、Audit、Decision | 修改状态 |
| Planner | 创建 Issue、依赖、Proposal | 直接改代码 |
| Worker | claim Issue、绑定 worktree、提交实现 | 审批自己的高风险改动 |
| Reviewer | 审查、请求修改、标记风险 | 绕过人类批准 high/critical |
| Test-Agent | 运行 allowlisted 验证、创建 Failure | 执行任意 shell |
| Explorer | 创建 proposal、draft issue | 直接执行生产修改 |
| Maintainer | 处理 low risk merge candidate | MVP 不允许远程自动合并 |
| Human/Admin | 审批、暂停、提升风险、覆盖决策 | 无 |

## 3. 风险等级

| 等级 | 例子 | 策略 |
|------|------|------|
| low | 文档、测试补充、小型展示调整 | 测试通过后可进入 merge candidate |
| medium | 普通业务逻辑、非核心状态字段 | Reviewer-Agent 通过后等待人工可选审批 |
| high | 任务市场、权限、worktree、数据库 schema | 必须人工审批 |
| critical | 自我修改、自动合并、权限绕过、数据删除 | 先提交设计 proposal，批准后才能实现 |

## 4. Intent Lock

Intent Lock 是用户当前意图的硬边界。

例子：

```text
本轮只做任务市场，不做 UI。
不修改 src/naumi_agent/model/*。
所有 high risk 任务先提交 proposal。
```

规则：

1. Agent claim 前必须检查 Intent Lock。
2. 修改路径命中 blocked path 时，只能进入 Proposal Mode。
3. risk_level 大于等于阈值时，只能进入 Proposal Mode。
4. Human 可以临时关闭或新增 Intent Lock。

## 5. Proposal Mode

Proposal Mode 是安全出口。

Agent 可以自由探索，但产物必须是：

```text
proposal
draft issue
risk analysis
test plan
comparison doc
draft PR without merge permission
```

Proposal 必须包含：

```text
目标
背景
影响范围
拟修改文件
风险等级
验证计划
需要人类确认的问题
```

Proposal 通过后，Planner 才能把它转为 executable Issue。

## 6. 审批策略

### 6.1 自动不需要审批

仅限：

- 文档更新。
- 新增测试但不改生产逻辑。
- 低风险格式修复。

仍需：

- 有 Validation Run。
- 有 Audit Event。
- 可回滚或可撤销。

### 6.2 必须审批

以下情况必须人工审批：

- high / critical risk。
- 修改权限、安全、预算、模型路由。
- 修改数据库 schema。
- 修改任务市场、lease、worktree 生命周期。
- 测试覆盖不足。
- Agent 自评信心低。
- Reviewer-Agent 标记风险。

## 7. 自动合并边界

MVP 不做自动合并。

允许的状态是：

```text
auto_merge_candidate
```

含义：

- 测试通过。
- 风险低。
- Reviewer-Agent 无阻塞意见。
- 仍等待 Human 或后续版本策略决定是否合并。

## 8. 失败治理

失败必须变成 Failure Card。

| FailureKind | 默认处理 |
|-------------|----------|
| lease_expired | 释放任务，保留 worktree |
| test_failed | 指派原 Agent 或 Test-Agent |
| merge_conflict | 创建 resolver issue |
| scope_violation | 暂停 Agent，要求 proposal |
| context_stale | 要求同步上下文 |
| permission_denied | 显示权限原因 |
| worktree_dirty | 阻止删除，要求 keep 或审查 |

## 9. Agent 行为账本

每个执行任务应尽量记录：

```text
读取文件
执行命令
修改文件
验证命令
失败调整
自评信心
已知风险
下一步状态
```

这些信息可以先进入 AuditEvent payload 或后续单独行为账本表。

## 10. 决策日志约束强度

| kind | 强度 | Agent 能否挑战 |
|------|------|----------------|
| principle | 很强 | 只能 proposal |
| architecture | 强 | 只能 proposal |
| policy | 强 | 只能 proposal |
| temporary | 中 | 可请求解除 |
| experiment | 弱 | 可提交结果和回滚建议 |

## 11. 安全红线

Agent 不允许：

1. 直接修改 main。
2. 删除 dirty worktree。
3. 绕过 allowlist 执行 shell。
4. 在未认领任务时修改生产文件。
5. 在 high/critical 风险下自动合并。
6. 在命中 Intent Lock 后继续执行代码修改。
7. 把 secret 写入 AuditEvent。

## 12. MVP 验收

治理层完成的最低标准：

- 可以创建 IntentLock。
- claim 会检查 active lease。
- high risk 会被标记为需要审批。
- 验证失败会创建 FailureCard。
- Proposal tool 只能记录建议，不直接改代码。
- Dashboard 能展示 pending approval / failure / decision。
