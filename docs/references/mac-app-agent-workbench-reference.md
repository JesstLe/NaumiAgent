# Mac App 多 Agent 工作台参考稿

> 来源：用户提供的产品设计参考内容。用途：作为 NaumiAgent Mac 应用方向的原始参考文档，后续正式方案应在此基础上提炼，而不是直接视为已定稿规格。

可以补充，而且我建议你把这个系统设计成 **“共享画板 + 任务市场 + Git 工作流 + 自动化验证 + 人类治理”** 的组合。

核心思想是：**不要让所有 agent 只是围着一个聊天窗口讨论，而是让它们围绕一个可操作的共享状态工作。** 这个共享状态就是你的大画板。

---

## 1. 总体形态：一个“可视化多 Agent 工作台”

你可以把 Mac App / 前端设计成类似：

**左侧：任务与 Issue 树**
展示用户布置的大任务、拆分出的子任务、依赖关系、优先级、阻塞状态。

**中间：大画板 Canvas**
所有 agent、任务卡片、文件变更、PR、测试结果、讨论节点都以可视化卡片存在。用户可以拖拽、连线、锁定、审批。

**右侧：详情面板**
选中某个 agent / issue / PR / commit / test run 后，显示它的上下文、行动记录、当前计划、日志、Diff、审批按钮。

**底部：事件时间线**
类似 GitHub Activity Feed，记录：

* Agent A 领取了任务
* Agent B 创建了 issue
* Agent C 推送了 branch
* CI 失败
* Reviewer agent 提出修改意见
* Human 批准合并
* Agent D 基于最新 main 继续下一步

这个系统本质上是一个 **Agent 操作系统的 UI**。

---

## 2. 最重要的设计：自由分工不能完全“自由”，要做成“受约束的自由”

多 agent 协同最容易出问题的地方是：大家都去做同一件事、互相覆盖、上下文冲突、无限讨论、不知道什么时候停。

所以我建议使用一种 **任务市场机制**。

用户发布一个大任务后，系统先生成一个顶层任务卡：

> “实现多 agent 协同研发系统”

然后由一个或多个 planner agent 将它拆成 issue：

* 设计共享画板数据模型
* 实现 agent 注册与状态管理
* 实现任务领取机制
* 实现 Git 分支隔离
* 实现自动测试流水线
* 实现 PR 审批流
* 实现前端 Canvas UI
* 实现 agent 活动日志
* 实现冲突检测与回滚

每个 issue 都要有一个标准结构：

```text
Issue ID
标题
背景
目标
验收标准
输入资料
预期产物
依赖任务
风险等级
是否需要人工审批
测试要求
当前状态
负责人 agent
相关 branch / PR / commit
```

然后 agent 不是随便开工，而是通过 **认领 / 竞标 / 租约** 的方式工作。

---

## 3. Agent 如何自由分工：推荐“认领 + 竞标 + 租约”模型

每个 agent 都有自己的能力画像：

```text
Agent Name: Frontend-Agent
能力: React, Canvas, UI 状态管理, 动效
偏好: UI 任务、交互任务
风险等级: 中
最大并发任务数: 1
```

```text
Agent Name: Backend-Agent
能力: API, 数据库, 权限系统, WebSocket
偏好: 后端架构、任务调度
最大并发任务数: 2
```

```text
Agent Name: Reviewer-Agent
能力: 代码审查、安全检查、架构一致性
偏好: Review, 测试覆盖率
```

当新的 issue 出现后，agent 可以提交一个 “bid”：

```text
我可以做这个任务
预计修改文件: packages/api/tasks/*
预计耗时: 中
信心: 0.82
风险: 中
依赖: 需要先完成 Agent Registry
计划: 先补数据模型，再实现 claim API，最后写测试
```

系统可以有一个 coordinator，但它不是传统意义上的老板，而是一个 **仲裁器**。它根据以下因素分配任务：

* agent 的能力匹配度
* 当前负载
* 历史成功率
* 任务依赖是否满足
* 是否有人已经认领
* 是否值得并行探索
* 风险等级
* 成本预算

任务一旦被认领，就进入 **lease 状态**：

```text
Issue #23 claimed by Backend-Agent
Lease: 45 minutes
Status: In Progress
```

如果 agent 长时间没有进展，lease 过期，任务回到任务市场。

这样 agent 有自由，但不会乱。

---

## 4. 允许并行，但要分清“实现型并行”和“探索型并行”

有些任务只能一个 agent 做，比如修改同一个核心 API。

但有些任务可以让多个 agent 同时探索，比如：

* 画板交互方案
* agent 分工算法
* PR 审批策略
* 测试架构
* UX 原型

我建议给任务加一个字段：

```text
parallel_mode:
  - exclusive     # 只能一个 agent 做
  - cooperative   # 多个 agent 可协作
  - competitive   # 多个 agent 各自提出方案，最后择优
  - exploratory   # 允许自由研究，但不能直接合并主分支
```

例如：

```text
Issue #10: 设计任务分配算法
parallel_mode: competitive
```

这时可以让三个 agent 各自提交方案：

* Agent A：中心调度模型
* Agent B：任务市场模型
* Agent C：黑板协作模型

最后由 Reviewer-Agent 或人类选择合并方案。

这能让系统真正具备“多 agent 智能协作”的感觉，而不是简单排队执行任务。

---

## 5. GitHub 式工作流：每个 agent 都应该有自己的 branch

我建议强制规定：

**任何 agent 不允许直接改 main。**

每个 agent 开始任务时，系统自动创建工作区：

```text
agent/backend-agent/issue-23-task-claim-api
```

Agent 的工作流：

```text
读取 issue
生成计划
创建 branch
修改代码
本地运行测试
提交 commit
推送 branch
创建 PR
附带总结、测试结果、风险说明
等待 review 或自动合并
```

PR 模板可以是：

```text
## 目标
解决 Issue #23：实现任务认领 API

## 修改内容
- 新增 claim_task endpoint
- 新增 task_lease 表
- 新增 lease expiration logic
- 增加单元测试

## 测试
- pnpm test passed
- task claim conflict test passed
- lease expiration test passed

## 风险
- 需要确认 lease duration 是否可配置
- 并发 claim 需要数据库唯一约束保护

## 下一步建议
- 增加 agent heartbeat
- 增加任务重新分配机制
```

这样你就能得到非常清晰的 agent 行动轨迹。

---

## 6. 自动化测试：不只是 CI，而是 Agent 的“合并许可证”

每个 PR 必须经过自动化验证。可以分几层：

### 第一层：静态检查

* TypeScript 类型检查
* lint
* format
* import cycle 检查
* secret scan
* dependency check

### 第二层：单元测试

* API 测试
* 工具函数测试
* 调度算法测试
* agent 状态机测试

### 第三层：集成测试

* 多 agent 同时 claim 一个任务
* 一个 agent lease 过期后任务被重新领取
* PR 合并后 dependent issue 自动解锁
* CI 失败后 PR 自动回到修改状态

### 第四层：仿真测试

这个很重要。

你可以设计一个 **simulation runner**，专门模拟多 agent 协同场景：

```text
Scenario: 5 agents receive 20 issues
Expected:
- no duplicate exclusive task ownership
- no deadlock
- no circular dependency
- all completed tasks have artifacts
- all merged PRs pass tests
```

这比普通 CI 更适合你的系统。

---

## 7. Agent 完成任务后：不要只有“停下”或“继续”两种，要有策略

你提到：

> 一个 agent 完成一项任务后，可以阻塞以等待审批，也可以自由探索下一步计划。

我建议设计成四种完成态。

### 1. `waiting_review`

任务完成，但需要审批。

适用于：

* 代码改动较大
* 涉及核心架构
* 涉及权限、安全、支付、数据删除
* 测试没有完全覆盖
* agent 置信度低
* 用户显式要求审批

### 2. `auto_merge_candidate`

任务低风险，测试通过，可以进入自动合并候选。

适用于：

* 文档改动
* 测试补充
* 小型 bug fix
* 类型修复
* 非核心 UI 微调

但也建议至少经过一个 Reviewer-Agent。

### 3. `continue_next_issue`

当前任务完成后，agent 自动从任务市场领取下一个未阻塞任务。

条件：

* 当前 PR 已合并，或者无需等待合并
* agent 没有达到预算上限
* 系统没有更高优先级人工审批
* 下一个任务依赖已满足

### 4. `explore_next_steps`

agent 不直接写生产代码，而是自由探索下一步。

这种模式非常有价值，但要限制权限。

探索型 agent 可以：

* 创建 proposal
* 创建 issue
* 写设计文档
* 生成测试用例
* 发现潜在 bug
* 提出重构建议
* 做技术调研
* 创建 draft PR

但不能直接合并代码。

也就是说，探索可以自由，落地必须受控。

---

## 8. 建议加一个“风险等级驱动”的审批系统

每个任务可以自动计算风险等级：

```text
risk_level:
  - low
  - medium
  - high
  - critical
```

风险判断可以基于：

* 修改文件数量
* 是否涉及核心模块
* 是否涉及认证、权限、账单、数据删除
* 是否修改数据库 schema
* 是否修改 agent 调度逻辑
* 测试覆盖是否充分
* agent 自评置信度
* review agent 评分
* 是否与其他 PR 冲突

对应策略：

| 风险等级     | 行为                                |
| -------- | --------------------------------- |
| low      | 测试通过 + reviewer agent 通过，可自动合并    |
| medium   | 测试通过 + reviewer agent 通过，进入人工可选审批 |
| high     | 必须人工审批                            |
| critical | 必须先提交设计方案，批准后才能实现                 |

这个机制能让系统既自动化，又不会失控。

---

## 9. 画板里的每个对象都应该是“可审计实体”

你的画板不是普通白板，而是一个状态机界面。

建议核心对象包括：

```text
Task Card
Issue Card
Agent Card
Branch Card
PR Card
Commit Card
Test Run Card
Decision Card
Artifact Card
Discussion Card
Risk Card
Approval Card
```

例如一个任务卡片可以连到：

```text
用户需求
  ↓
Planner 拆出的 issue
  ↓
Backend-Agent 的 branch
  ↓
commit
  ↓
PR
  ↓
CI test run
  ↓
Reviewer-Agent comment
  ↓
Human approval
  ↓
merged
  ↓
解锁下游任务
```

这样用户在画板上能看到整个因果链。

这非常关键，因为多 agent 系统最大的问题不是“能不能做”，而是“人类能不能理解它们为什么这么做”。

---

## 10. Agent 之间的通信：不要只靠聊天，要靠结构化事件

Agent 之间可以聊天，但核心协作要靠事件总线。

事件例子：

```text
task.created
task.claimed
task.blocked
task.unblocked
task.completed
issue.created
branch.created
commit.pushed
pr.opened
pr.review_requested
pr.approved
pr.rejected
ci.started
ci.failed
ci.passed
merge.completed
conflict.detected
human.approval_requested
```

每个 agent 都订阅自己关心的事件。

例如：

* Frontend-Agent 订阅 `ui.*`, `api.changed`, `design.approved`
* Backend-Agent 订阅 `schema.changed`, `task.claimed`, `api.required`
* Reviewer-Agent 订阅 `pr.opened`, `ci.failed`
* Planner-Agent 订阅 `task.created`, `task.completed`, `dependency.blocked`
* Test-Agent 订阅 `pr.opened`, `feature.completed`

这样多 agent 协同就从“聊天式”变成“事件驱动式”。

---

## 11. 需要一个共享记忆，但要分层

所有 agent 都能看到用户布置的大任务，但不能每次都把全部上下文塞给所有 agent，否则成本高、噪音大、容易混乱。

建议分四层记忆：

### 1. Global Mission

用户最初的目标、产品方向、最高优先级。

所有 agent 都可见。

### 2. Project State

当前 issue、PR、分支、测试、依赖、风险。

所有 agent 可查询。

### 3. Local Task Context

某个 agent 正在处理的任务上下文。

只给相关 agent。

### 4. Decision Log

已经做过的架构决策，所有 agent 必须遵守。

例如：

```text
Decision #12:
任务认领必须通过 lease 机制，不能只用 status 字段。
原因：避免 agent 崩溃后任务永久占用。
日期：2026-06-26
批准者：Human
```

Decision Log 很重要。否则 agent 会反复推翻之前的设计。

---

## 12. 我建议加入一个“冲突仲裁器”

多 agent 并行开发一定会冲突。

冲突分几类：

### 代码冲突

两个 agent 改同一个文件。

解决方式：

* 自动 rebase
* 自动 merge
* 冲突过大则创建 conflict issue
* 指派 Resolver-Agent

### 设计冲突

两个 agent 对同一模块提出不同架构。

解决方式：

* 生成 comparison doc
* Reviewer-Agent 打分
* 人类选择
* 选定方案进入 Decision Log

### 任务冲突

两个 agent 领取了类似任务。

解决方式：

* 合并 issue
* 一个继续实现，一个转为 reviewer
* 或者允许 competitive mode

### 依赖冲突

一个 agent 需要另一个 agent 的产物。

解决方式：

* 自动标记 blocked
* 订阅对方 PR merge event
* merge 后自动唤醒

这个仲裁器可以是一个独立 agent，也可以是 orchestrator 的一部分。

---

## 13. Agent 权限需要分级

不要让所有 agent 都有同样权限。

建议权限模型：

```text
Observer
只能读画板、读 repo、读 issue。

Planner
可以创建 issue、拆任务、标依赖。

Worker
可以领取任务、创建 branch、提交 PR。

Reviewer
可以 review PR、请求修改、标记风险。

Maintainer
可以自动合并低风险 PR。

Explorer
可以创建 proposal / draft issue，但不能改主分支。

Admin / Human
可以覆盖任何决策。
```

权限控制是这个系统能不能长期稳定运行的关键。

---

## 14. 你还应该加入“预算”和“停止条件”

多 agent 系统如果没有停止条件，很容易无限探索。

每个任务都应该有：

```text
max_steps
max_tokens
max_runtime
max_cost
definition_of_done
stop_condition
```

例如：

```text
Issue #31: 设计画板状态机

Definition of Done:
- 提交状态机图
- 列出所有状态
- 列出所有转移条件
- 给出至少 5 个异常场景
- 创建实现 issue

Stop condition:
- 不直接实现代码
- 不继续扩展到权限系统
```

这样 agent 不会做着做着跑偏。

---

## 15. 可以增加一个“下一步建议池”

当 agent 完成任务后，它经常会发现额外问题。

但这些问题不应该直接变成当前任务的一部分，否则 scope 会失控。

所以可以设计一个 `Next Step Pool`：

```text
Agent 发现的问题：
- claim API 缺少 rate limit
- task lease duration 应该可配置
- Canvas 节点太多时需要虚拟化
- PR review 应该支持风险评分
```

这些建议先进入池子，由 Planner-Agent 或人类定期整理成正式 issue。

这能让 agent 自由探索，同时保持主任务干净。

---

## 16. 前端画板的交互设计建议

画板里可以有几种视图模式。

### Mission View

展示用户目标、总体进度、当前阻塞点。

适合用户看全局。

### Agent View

展示每个 agent 在做什么：

```text
Frontend-Agent
状态: working
当前任务: Issue #18 Canvas 节点拖拽
Branch: agent/frontend/issue-18-canvas-drag
最近动作: pushed commit 3 minutes ago
下一步: run UI tests
```

### Dependency View

展示任务依赖图。

适合看哪些任务被阻塞。

### Git View

展示 branch、commit、PR、merge 状态。

适合研发工作流。

### Risk View

展示高风险改动、待审批 PR、失败测试。

适合人类介入。

### Timeline View

展示所有 agent 的事件流。

适合审计和回放。

---

## 17. 后端架构可以这样分

你可以把系统拆成这些核心服务：

```text
Canvas Service
负责画板节点、边、布局、实时同步。

Agent Registry
管理 agent 身份、能力、权限、状态、心跳。

Task Service
管理任务、issue、依赖、认领、lease、阻塞状态。

Orchestrator
负责事件分发、调度、权限检查、策略执行。

Repo Service
封装 Git 操作、branch、commit、PR、merge。

CI Service
运行测试、收集报告、判断是否允许合并。

Review Service
负责 agent review、人类审批、风险评分。

Memory Service
管理项目记忆、决策记录、上下文摘要。

Audit Service
记录所有事件、行动、审批、回滚。
```

数据流大概是：

```text
用户发布任务
  ↓
Task Service 创建 mission
  ↓
Planner-Agent 拆 issue
  ↓
Task Service 发布 issue.created
  ↓
Worker-Agent 竞标 / 认领
  ↓
Repo Service 创建 branch
  ↓
Agent 修改并提交
  ↓
CI Service 测试
  ↓
Review Service 审查
  ↓
Human / Policy 审批
  ↓
Repo Service 合并
  ↓
Canvas 更新状态
  ↓
Planner-Agent 解锁下一批任务
```

---

## 18. 一个具体例子

用户在画板上写：

> 做一个多 agent 协同开发系统。

系统自动生成：

```text
Mission #1
目标：构建多 agent 协同开发系统
状态：Planning
```

Planner-Agent 创建 issue：

```text
Issue #1: 定义 agent 状态机
Issue #2: 设计任务认领机制
Issue #3: 实现 Git branch 隔离
Issue #4: 实现 PR 审批流
Issue #5: 实现 Canvas UI
Issue #6: 实现事件时间线
Issue #7: 实现自动测试
```

Backend-Agent 领取 Issue #2。

Frontend-Agent 领取 Issue #5。

Test-Agent 订阅所有 PR。

Reviewer-Agent 等待 PR。

Backend-Agent 完成后推送：

```text
branch: agent/backend/issue-2-task-claim
commit: add task claim and lease model
PR: #14
CI: passed
Risk: medium
```

Reviewer-Agent 评论：

```text
发现并发 claim 时可能有竞态条件。
建议增加数据库唯一约束和事务测试。
```

Backend-Agent 修复后再次推送。

CI 通过。

系统判断：

```text
risk_level: medium
requires_human_review: true
```

画板上出现审批卡片：

```text
PR #14 等待审批
[查看 Diff] [批准合并] [要求修改] [转为高风险]
```

用户批准后，系统合并 PR，并自动解锁：

```text
Issue #8: 实现 agent heartbeat
Issue #9: 实现 lease expiration recovery
```

Backend-Agent 可以自动领取下一个，也可以进入探索模式提出新 issue。

---

## 19. 我认为还必须补充的能力

### A. 回滚机制

每次自动合并都要能回滚。

```text
merge_id
previous_main_sha
rollback_command
affected_issues
affected_agents
```

当测试后续失败，系统可以自动创建 rollback PR。

---

### B. 快照与回放

用户应该能回放一次多 agent 协作过程：

```text
10:01 用户创建任务
10:02 Planner 拆分任务
10:03 Backend-Agent 领取 issue
10:04 Frontend-Agent 领取 issue
10:12 Backend-Agent 推送 PR
10:14 CI failed
10:16 Backend-Agent 修复
10:19 Human approved
10:20 Merged
```

这对调试多 agent 行为非常重要。

---

### C. Agent 自评 + 他评

每个 agent 完成任务后，要写自评：

```text
confidence: 0.78
known_risks:
  - 没有覆盖极端并发场景
  - UI 尚未接入真实 WebSocket
needs_review_from:
  - Backend-Agent
  - Security-Agent
```

Reviewer-Agent 再给他评：

```text
correctness: 0.84
test_coverage: 0.71
architecture_fit: 0.9
merge_recommendation: request_changes
```

久而久之，你可以统计每个 agent 的可靠性。

---

### D. Agent 不应该只会写代码，还要会“创造工作”

优秀的 agent 系统不是只执行 issue，而是会发现：

* 需求不清楚
* 测试缺失
* 架构债务
* 依赖阻塞
* 文档不同步
* UI 状态不一致
* 用户体验问题

所以 agent 应该能创建 issue，但创建 issue 要分级：

```text
suggested_issue      # 建议
draft_issue          # 草稿
approved_issue       # 已批准，可执行
blocking_issue       # 阻塞当前任务
```

这样不会让 issue 爆炸。

---

### E. Human Control Layer

人类不应该只在最后审批，而是可以随时介入：

* 暂停某个 agent
* 终止某个任务
* 锁定某个文件
* 禁止自动合并
* 提升风险等级
* 改变优先级
* 合并 / 拆分 issue
* 指定某个 agent 做某个任务
* 要求所有 agent 重新同步上下文

这会让产品更可控。

---

## 20. 最推荐的 MVP 范围

第一版不要做太大。我建议 MVP 做这些：

```text
1. 大画板展示 mission、issue、agent、PR、test run
2. 用户创建任务
3. Planner-Agent 自动拆 issue
4. Worker-Agent 通过 claim/lease 领取任务
5. 每个 agent 独立 branch
6. agent 提交 PR
7. 自动测试
8. Reviewer-Agent review
9. 人类审批合并
10. 合并后自动更新画板状态
```

先不要急着做：

* 复杂竞争式分工
* 完全自治合并
* 多 repo 支持
* 大规模 agent swarm
* 自动线上部署
* 自我修改 agent 系统

MVP 的目标应该是证明：

> 多个 agent 可以围绕一个共享画板，安全地拆任务、领任务、写代码、提交 PR、测试、review、等待审批、继续下一步。

---

## 21. 我会给这个系统一个核心协议

可以叫：

```text
Agent Collaboration Protocol, ACP
```

它规定所有 agent 必须遵守：

```text
1. 不直接修改 main
2. 不执行未认领任务
3. 每个任务必须有验收标准
4. 每个代码改动必须关联 issue
5. 每个 PR 必须有测试结果
6. 高风险任务必须人工审批
7. 探索结果必须以 proposal / issue / draft PR 形式提交
8. 所有决策必须写入 Decision Log
9. 所有行动必须进入 Audit Log
10. agent 完成任务后必须声明下一步状态
```

这套协议比具体模型更重要。

---

## 22. 总结成一句话

你要做的不是“多个 agent 一起聊天写代码”，而是一个 **可视化、可审计、可回滚、带任务市场和 Git 工作流的自治研发平台**。

我建议最核心的设计是：

```text
共享画板 = 当前世界状态
Issue = 可执行任务单位
Agent = 有能力画像和权限的执行者
Claim/Lease = 自由分工机制
Branch/PR = 隔离与提交机制
CI/Test = 合并许可证
Review/Approval = 风险控制
Decision Log = 长期一致性
Audit Log = 可解释性
Next Step Pool = 自由探索边界
```

这样 agent 可以自由分工，但不会失控；可以自动推进，但人类仍然能理解、审批和回滚。

---

## 23. 针对 NaumiAgent Mac App 的补充设计

在 NaumiAgent 的当前阶段，这个 Mac App 不应该先做成一个云端平台，也不应该只是聊天窗口套壳。更合适的第一形态是一个 **local-first 的 Agent 研发控制台**：默认管理本机 workspace、Git worktree、终端进程、测试进程、本地记忆库、审计日志和人类审批。

### 23.1 Local-first 边界

第一版应优先服务单机研发场景：

```text
本机 workspace
  ↓
本地 NaumiAgent API / Bridge
  ↓
本地任务市场、worktree、验证进程、审计日志
  ↓
Mac App 可视化工作台
```

云同步、多人团队、多机器 agent 调度、多 repo 平台化可以后置。这样更符合 NaumiAgent 当前的基础设施状态，也更容易验证“多 agent 能否安全协作研发”这个核心命题。

### 23.2 Worktree 沙箱必须是一等对象

参考稿中提到每个 agent 独立 branch。对本项目来说还应该更进一步：每个执行型 agent 的任务租约应绑定到一个独立 Git worktree。

推荐关系：

```text
Mission
  -> Issue
    -> Agent Lease
      -> Git Worktree
        -> Branch
        -> Validation Run
        -> Patch / PR
```

画板上不只显示 branch，还要显示 worktree 状态：

```text
clean        # 可清理
dirty        # 有未提交修改
kept         # 人工保留待审查
conflicted   # rebase / merge 冲突
missing      # 目录丢失，需要修复状态
```

这样可以避免多个 agent 在同一个工作目录里互相污染，也方便人类审查、保留、删除或回滚 agent 的工作。

### 23.3 增加“人类意图锁”

除了文件锁和任务锁，还需要一个更高层的 `Intent Lock`。

例子：

```text
当前 mission 约束：
- 本轮只设计任务市场，不实现 UI
- 不修改数据库 schema
- 不触碰 src/naumi_agent/model/*
- 所有风险等级 high 的任务先提交 proposal
```

所有 agent 在领取任务、创建 issue、修改文件、提交 PR 前，都必须检查当前 mission 的意图锁。这样可以防止 agent 自行扩大 scope。NaumiAgent 的长期目标是自进化，但自进化必须受人类当前意图约束。

### 23.4 Agent 行为账本

Audit Log 不应只记录“发生了什么”，还要记录 agent 为什么这样做。

每个 agent 的行为账本建议记录：

```text
读取了哪些文件
为什么选择这些文件
提出了哪些假设
执行了哪些命令
修改了哪些文件
运行了哪些验证
失败后如何调整
留下了哪些不确定性
```

这对两个方向很关键：

1. 人类可以回放和审计 agent 的决策链。
2. NaumiAgent 未来可以基于真实行为证据做自我审查，而不是只评估最终回答。

### 23.5 上下文健康度可视化

多 agent 系统经常失败不是因为代码能力不足，而是因为上下文过期、污染、缺失或过载。

Mac App 应该展示每个 agent 的 `Context Health`：

```text
good        # 已同步任务、决策、最新代码和测试状态
stale       # 长时间未同步 main / Decision Log / 任务状态
overloaded  # 上下文太大，需要压缩或分层
missing     # 缺少关键输入资料、验收标准或依赖产物
conflicted  # agent 当前计划与意图锁或决策日志冲突
```

当 agent 要继续执行时，系统可以要求它先同步最新 Decision Log、相关 PR、失败测试和目标约束，再进入执行。

### 23.6 失败状态机

失败不应该只是红色报错，而应该是画板上的一等对象。

建议创建 `Failure Card`：

```text
类型: test_failed
影响: PR #14 blocked
来源: Validation Run #8
建议动作:
- 重新运行测试
- 指派原 Agent 修复
- 指派 Test-Agent 诊断
- 转为人工处理
```

需要覆盖的失败类型：

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

每种失败都要有清晰的下一步动作，而不是只把日志丢给用户。

### 23.7 Decision Log 需要约束强度

Decision Log 不能只是普通笔记。每条决策都应该带约束类型：

```text
principle     # 长期原则，例如不直接改 main
architecture  # 架构决策，例如任务认领必须 lease
policy        # 治理策略，例如 high risk 必须人工审批
temporary     # 临时约束，例如本轮不动 API schema
experiment    # 实验性决策，可回滚
```

agent 行动前必须读取强约束；如果它想挑战 `principle`、`architecture` 或 `policy`，不能直接执行，只能提交 proposal。

### 23.8 Proposal Mode 作为安全出口

第一版就应该有 `Proposal Mode`。很多 agent 不应该直接写生产代码，但可以安全地产出：

```text
设计提案
风险分析
实现计划
测试计划
对比报告
待确认问题
draft issue
draft PR
```

这能让探索保持自由，同时让落地受控。对 NaumiAgent 的自进化路线尤其重要：先让 agent 学会提出可审查的改进，再让它获得有限执行权。

### 23.9 Mac App 第一屏应该是 Mission Dashboard

不要把聊天窗口放在第一视觉中心。第一屏应该回答五个问题：

```text
系统现在在做什么？
哪些 agent 正在运行？
哪些任务被阻塞？
哪些 PR / patch 等待审批？
有哪些风险需要人类处理？
```

推荐第一屏模块：

```text
Mission Summary
Active Agents
Blocked Issues
Pending Approvals
Failed Validations
Recent Decisions
Next Step Pool
```

聊天仍然存在，但更适合作为右侧详情面板或底部输入区。主体验应该是“治理一个自治研发系统”，不是“和多个 agent 聊天”。
