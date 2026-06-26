# NaumiAgent Mac Agent Workbench PRD

> 参考输入：`docs/references/mac-app-agent-workbench-reference.md`  
> 执行计划：`docs/superpowers/plans/2026-06-27-mac-agent-workbench-mvp.md`

## 1. 产品定位

NaumiAgent Mac Agent Workbench 是一个 **local-first 的多 Agent 研发治理工作台**。

它不是聊天壳，也不是云端项目管理工具。它的第一目标是让用户在本机 workspace 中可视化、审计和治理多个 Agent 的研发行为：

```text
用户目标
  -> Mission
  -> Issue / Task
  -> Agent Claim / Lease
  -> Git Worktree / Branch
  -> Validation Run
  -> Failure / Review / Approval
  -> Merge Candidate / Next Step
```

核心产品判断：

1. 用户真正需要的不是“更多 Agent 聊天”，而是“知道 Agent 正在做什么、为什么这么做、什么时候需要我介入”。
2. 多 Agent 自由协作必须建立在可操作的共享状态上，而不是建立在对话历史上。
3. NaumiAgent 的自进化愿景必须先经过人类治理层：意图锁、风险审批、审计账本、失败状态机。

## 2. 目标用户

### 2.1 主要用户

独立开发者、研究型工程师、Agent 系统构建者。

他们的典型状态：

- 同时维护一个复杂代码库和多条研发思路。
- 希望 Agent 能拆任务、写代码、跑测试、提出改进。
- 不希望 Agent 直接失控修改主分支。
- 需要清楚看到每个 Agent 的工作证据和风险。

### 2.2 次要用户

小团队技术负责人。

他们可能后续需要多人协作、远程仓库 PR、云端同步，但 MVP 不优先服务这些场景。

## 3. 用户问题

| 问题 | 当前表现 | Workbench 解决方式 |
|------|----------|--------------------|
| 多 Agent 状态不可见 | 用户只看到聊天输出，不知道谁在做什么 | Mission Dashboard 显示 Agent、Issue、Lease、Worktree、Validation |
| 并行协作互相覆盖 | 多个 Agent 可能改同一工作区 | 每个执行任务绑定独立 Git worktree |
| Agent 扩大 scope | 原本只做一件事，Agent 自行扩展到多个模块 | Mission 级 Intent Lock 限制执行边界 |
| 失败不可处理 | 测试失败或冲突只是一段日志 | Failure Card 给出影响范围和下一步动作 |
| 决策反复摇摆 | Agent 忘记之前的架构选择 | Decision Log 成为行动前必须读取的约束 |
| 人类只能事后检查 | 用户直到最后才看到一大坨 diff | 风险等级、审批卡片、Proposal Mode 提前介入 |

## 4. MVP 目标

第一版要证明：

> 多个 Agent 可以围绕一个本地共享工作台，安全地拆任务、认领任务、绑定 worktree、运行验证、记录失败、等待审批，并让人类随时理解和接管。

MVP 必须支持：

1. 创建 Mission，并展示总体状态。
2. 将 Mission 拆成 Issue/Task，并记录验收标准。
3. Agent 通过 claim/lease 认领任务。
4. 执行型任务绑定独立 worktree。
5. 系统记录 Decision、Intent Lock、Audit Event。
6. 验证命令产生 Validation Run。
7. 验证失败产生 Failure Card。
8. Dashboard 汇总 mission、task、issue、failure、event。
9. 前端协议能承载 workbench snapshot/event。

## 5. 非目标

MVP 不做：

- 云端同步。
- 多机器 Agent 调度。
- 多 repo 平台化管理。
- 自动部署生产环境。
- 远程 GitHub PR 全流程。
- 完全自治合并。
- 原生 macOS 安装包。

这些不是永远不做，而是必须等本地协作内核稳定后再做。

## 6. 核心场景

### 6.1 创建 Mission

用户输入：

```text
做一个 Mac App，让我管理多个 Agent 协同研发 NaumiAgent。
```

系统创建：

```text
Mission: Mac Agent Workbench
Goal: 可视化治理本地多 Agent 研发流程
Status: planning
```

用户应看到：

- 当前目标。
- Planner-Agent 是否正在拆任务。
- 已生成的 Issue 数量。
- 是否存在缺失验收标准的 Issue。

### 6.2 Agent 认领任务

Backend-Agent 领取 `Issue #2: 实现任务市场`。

系统必须：

- 检查 Issue 是否被其他 Agent 认领。
- 检查依赖任务是否完成。
- 创建 active lease。
- 将 TaskStore 状态改为 `in_progress`。
- 记录 audit event。
- 可选绑定 worktree。

用户应看到：

```text
Issue #2
Owner: Backend-Agent
Lease: active, expires at 14:45
Worktree: issue-2-market
Risk: medium
```

### 6.3 验证失败

Test-Agent 或系统运行：

```text
pytest tests/unit/test_workbench_market.py -q
```

如果失败，系统必须创建 Failure Card：

```text
Failure: test_failed
Source: Validation Run #8
Affected Issue: #2
Suggested Actions:
- 指派原 Agent 修复
- 指派 Test-Agent 诊断
- 转人工处理
```

用户不应该只看到原始日志。

### 6.4 人类审批

当高风险任务完成后，系统创建审批卡：

```text
Approval Required
Risk: high
Reason: 修改任务市场和 lease 状态机
Actions:
- 查看 diff
- 批准进入 merge candidate
- 要求修改
- 提升为 critical
```

MVP 可以先记录审批状态，不要求自动合并远程 PR。

## 7. 第一屏信息架构

Mac App 第一屏是 Mission Dashboard，不是聊天窗口。

推荐模块：

```text
Mission Summary
Active Agents
Task Market
Active Leases
Worktrees
Pending Approvals
Failed Validations
Recent Decisions
Audit Timeline
Next Step Pool
```

聊天入口可以存在，但应该是底部输入区或右侧详情面板的一部分。

## 8. 成功指标

### 8.1 功能指标

- 用户能从 Dashboard 看出当前系统在做什么。
- 每个执行任务都有 owner、lease、risk、validation 状态。
- claim 冲突会被拒绝。
- lease 过期会释放任务。
- 测试失败会生成 Failure Card。
- 高风险任务不会绕过人工治理。

### 8.2 工程指标

- Workbench 数据模型有单元测试。
- Task market 有并发/冲突/过期测试。
- API contract 有后端测试。
- terminal-ui protocol 有 JS 测试。
- E2E 场景能 replay workbench snapshot。

### 8.3 用户体验指标

- 用户无需阅读日志即可知道失败原因和下一步。
- 用户能暂停、审查或拒绝 Agent 的工作。
- 用户能追溯一个任务从创建到验证的因果链。

## 9. 发布准入

MVP 可发布到内部自用时，必须满足：

- `ruff check src/ tests/` 通过目标范围。
- workbench 单元测试全通过。
- terminal-ui protocol 测试全通过。
- 至少一个真实本地 repo 场景跑通：Mission -> Issue -> Claim -> Worktree -> Validation -> Failure/Snapshot。
- 文档包含 PRD、架构、领域模型、事件协议、治理策略、测试策略。
