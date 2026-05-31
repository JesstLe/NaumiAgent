# NaumiAgent 优化规划：自主执行闭环强化

> 撰写日期：2026-06-01
>
> 目标：把 NaumiAgent 从“能力丰富的 Agent 框架”推进到“能安全隔离、持续执行、可验证完成、自主演进”的 Agent 系统。

---

## 1. 总体原则

本轮优化不追求一次性重构全项目，而是围绕一条主线逐步推进：

**隔离执行 → 后台执行 → 定时唤醒 → 目标追踪 → 自我修改 → 自我审查**

每个阶段必须独立交付、独立测试、独立验证。禁止把多个独立能力揉成一个大提交。

### 必须遵守

- 每个新能力同时支持用户手动触发和 Agent 自主调用。
- 用户命令和 Agent 工具必须复用同一套底层 service/execute 逻辑。
- 所有用户可见文案使用中文。
- 每个功能必须覆盖正常路径、错误路径、边界输入和真实场景。
- 每个功能完成后立即做自我审视，并记录不足。
- 每个功能通过验证后单独提交。

### 验证链路

每个阶段完成前必须通过：

```bash
ruff check src/ tests/
pytest tests/ -x
```

涉及具体模块时，还必须运行对应定向测试和至少一个真实场景演练。

---

## 2. 阶段一：Worktree 隔离执行

### 目标

为自修改、自进化、复杂子任务和并行 Agent 执行提供隔离工作区，避免直接污染主工作区。

### 新增模块

```text
src/naumi_agent/worktree/
├── __init__.py
├── manager.py
├── models.py
└── tools.py
```

### 核心能力

- 创建隔离 worktree。
- 将 task 与 worktree 绑定。
- 查询 worktree 状态。
- 拒绝删除有未提交改动的 worktree。
- 支持保留 worktree 供人工审查。
- 支持安全移除空净 worktree。
- 记录 worktree 生命周期事件。

### 工具接口

- `worktree_create`
- `worktree_status`
- `worktree_bind_task`
- `worktree_keep`
- `worktree_remove`

### 手动入口

CLI/TUI 增加对应斜杠命令：

- `/worktree create <名称> [任务ID]`
- `/worktree status [名称]`
- `/worktree keep <名称>`
- `/worktree remove <名称>`

### 验收标准

- 非法名称被拒绝，错误提示清晰。
- 不存在的 task 不能绑定。
- 有未提交文件时默认拒绝删除。
- 所有 git 操作失败时返回可读错误。
- worktree 状态能展示分支、路径、未提交文件数。
- Agent 可以通过 tool 自主创建并使用 worktree。

### 真实场景验证

1. 创建一个 task。
2. 创建绑定该 task 的 worktree。
3. 在 worktree 中修改文件。
4. 验证 `worktree_remove` 默认拒绝删除。
5. 调用 `worktree_keep` 保留供审查。

---

## 3. 阶段二：后台任务系统

### 目标

让长时间运行的命令不阻塞主循环。Agent 发起长任务后可以继续推理，任务完成后结果自动回注上下文。

### 新增模块

```text
src/naumi_agent/background/
├── __init__.py
├── models.py
├── store.py
├── runner.py
└── tools.py
```

### 核心能力

- 后台执行 shell 命令或工具任务。
- 查询任务状态。
- 取消运行中的任务。
- 保存完整输出为 artifact。
- 回注完成通知到 engine。
- TUI/API 展示后台任务列表。

### 工具接口

- `background_run`
- `background_status`
- `background_list`
- `background_cancel`
- `background_read_output`

### 验收标准

- `pytest`、`npm install`、`docker build` 等慢命令可后台运行。
- 主 agent loop 不被阻塞。
- 输出过长时只回注摘要，完整内容写入 artifact。
- 失败任务保留 exit code、stderr 摘要和完整日志路径。
- 取消任务能终止子进程及其子进程组。

### 真实场景验证

1. 后台运行 `python -c "import time; time.sleep(2); print('done')"`.
2. 主循环立即得到任务 ID。
3. 查询状态从 running 变为 completed。
4. 完成通知被注入下一轮上下文。

---

## 4. 阶段三：Scheduler / Reminder 定时唤醒

### 目标

支持 Agent 被时间触发，能够执行定期自审查、提醒、巡检和长期任务推进。

### 新增模块

```text
src/naumi_agent/scheduler/
├── __init__.py
├── models.py
├── store.py
├── cron.py
├── runner.py
└── tools.py
```

### 核心能力

- 一次性提醒。
- cron 周期任务。
- SQLite 持久化。
- 到点后创建 task 或注入 session message。
- 支持启用、暂停、取消。

### 工具接口

- `schedule_create`
- `schedule_list`
- `schedule_cancel`
- `schedule_pause`
- `schedule_resume`

### 验收标准

- cron 表达式严格校验。
- 过期一次性任务不会重复触发。
- 周期任务不会在同一分钟重复触发。
- 重启进程后 durable schedule 仍然存在。
- 用户能看到下一次触发时间。

### 真实场景验证

1. 创建一分钟后的提醒。
2. 等待触发。
3. 验证 session 中出现调度消息或创建了 task。
4. 重启后验证 durable schedule 仍能恢复。

---

## 5. 阶段四：Pursuit 目标追踪强化

### 目标

把 `/pursue` 从“循环尝试”强化为“基于验收标准持续执行，直到真实达成或明确阻塞”。

### 优化点

- 每个 goal 自动生成 success criteria。
- 每个 iteration 记录 plan、action、evidence、verification。
- 达成条件必须来自真实工具结果。
- 失败时基于证据生成下一步。
- 连续相同阻塞达到阈值后才标记 blocked。
- 完成后自动生成 self-review。

### 验收标准

- 能处理多步编码任务。
- 能运行验证命令并根据结果调整。
- 不因为模型口头声称完成就结束。
- 最终报告包含证据链、验证命令和剩余风险。

### 真实场景验证

让 `/pursue` 执行一个小型代码修复任务：

1. 读代码。
2. 修改文件。
3. 运行定向测试。
4. 失败后继续修复。
5. 测试通过后结束。

---

## 6. 阶段五：自修改与自进化接入隔离层

### 目标

让 `self_modify`、`self_evolve`、`hotreload` 默认在隔离 worktree 中运行，主工作区只接收经过验证的变更。

### 优化点

- `self_modify` 增加 `isolation_mode` 参数。
- 默认创建临时 worktree。
- 修改后在 worktree 中运行 ruff、import test、相关 pytest。
- 验证失败自动保留 worktree 并标记失败。
- 验证成功后输出合并建议，而不是直接污染主分支。

### 验收标准

- 被保护文件不能修改。
- 验证失败自动回滚或保留隔离现场。
- 成功结果包含 diff、测试结果、风险说明。
- 主工作区不因失败自修改产生脏改动。

### 真实场景验证

1. 对一个低风险工具文件做小改动。
2. 在 worktree 中完成修改和验证。
3. 验证主工作区无污染。
4. 输出可审查 diff。

---

## 7. 阶段六：拆分 `tools/analysis.py`

### 目标

降低单文件复杂度，让分析工具可测试、可演进、可按需加载。

### 拆分策略

先不改变外部工具名，只改变内部文件组织。

```text
src/naumi_agent/tools/analysis/
├── __init__.py
├── common.py
├── chaos.py
├── scale.py
├── state.py
├── eval.py
├── graph.py
├── self_review.py
└── registry.py
```

### 执行顺序

1. 先抽 `common.py`：路径解析、源码读取、router 调用、公共格式化。
2. 再迁移低耦合工具：chaos、scale、state。
3. 每迁移一个工具，保持原 tool name 不变。
4. 最后用 `registry.py` 聚合 `create_analysis_tools()`。

### 验收标准

- 外部调用不变。
- 每个工具有独立单测。
- CLI/TUI 斜杠命令不受影响。
- 拆分后 `analysis.py` 只保留兼容导出或删除。

---

## 8. 阶段七：双通道协议审计

### 目标

确保所有关键能力都同时支持：

- 用户手动触发：CLI/TUI 斜杠命令。
- Agent 自主调用：Tool 注册。

### 审计范围

- 分析工具。
- task 工具。
- memory 工具。
- subagent 工具。
- browser 工具。
- self_modify/self_evolve/hotreload。
- 新增 worktree/background/scheduler。

### 验收标准

- 每个能力有唯一底层 service/execute。
- CLI/TUI 只是薄封装。
- Tool 只是薄封装。
- 中文文案一致。
- 错误路径一致。

---

## 9. 推荐实施顺序

| 顺序 | 阶段 | 原因 |
|------|------|------|
| 1 | Worktree 隔离执行 | 直接降低自修改风险，是后续自进化基础 |
| 2 | 后台任务系统 | 改善长验证、长扫描、构建测试体验 |
| 3 | Scheduler / Reminder | 支持长期自主任务和定期自审查 |
| 4 | Pursuit 强化 | 把目标追踪变成真实完成闭环 |
| 5 | 自修改接入隔离层 | 让自进化能力安全落地 |
| 6 | 拆分 analysis.py | 降低维护成本，避免巨型文件继续膨胀 |
| 7 | 双通道协议审计 | 收束用户入口和 Agent 工具入口 |

---

## 10. 风险与控制

### 风险：与其他线程冲突

控制方式：

- 每阶段尽量新增模块。
- 不批量格式化全仓库。
- 不修改无关测试。
- 开始编码前先看 `git status --short`。

### 风险：功能半成品进入主线

控制方式：

- 每阶段必须有定向单测。
- 每阶段必须有真实场景验证。
- 每阶段必须能单独回滚。

### 风险：工具变成 prompt 套壳

控制方式：

- 每个工具必须有真实代码逻辑。
- 分析工具必须有静态扫描阶段。
- 调度、后台、worktree 必须直接操作真实状态。

### 风险：用户体验变差

控制方式：

- 所有错误提示中文化。
- 每个长任务提供状态反馈。
- 每个危险动作提供明确拒绝原因。
- 每个完成结果包含下一步建议或审查入口。

---

## 11. 第一阶段详细任务清单

建议从 Worktree 隔离执行开始。

### Step 1：数据模型

- 定义 `WorktreeRecord`。
- 定义 `WorktreeStatus`。
- 定义生命周期事件结构。

### Step 2：Manager

- 实现名称校验。
- 实现 git worktree create/status/remove。
- 实现 dirty check。
- 实现 task 绑定。
- 实现事件日志。

### Step 3：Tools

- 实现 `worktree_create`。
- 实现 `worktree_status`。
- 实现 `worktree_bind_task`。
- 实现 `worktree_keep`。
- 实现 `worktree_remove`。

### Step 4：Engine 注册

- 在 engine 初始化时注册 worktree tools。
- 注入 task store 或必要配置。

### Step 5：CLI/TUI 入口

- 增加 `/worktree` 命令。
- TUI 展示中文反馈。

### Step 6：测试

- 单测名称校验。
- 单测 dirty worktree 拒绝删除。
- 单测不存在 task 绑定失败。
- 单测 tool 参数校验和中文错误。
- 集成测试真实 git worktree 生命周期。

### Step 7：真实演练

在本仓库中创建临时 task 和临时 worktree，执行一次完整生命周期，然后清理或保留审查。

---

## 12. 完成定义

本规划不是以“代码写完”为完成，而是以以下条件为完成：

- 每个阶段都有独立模块和测试。
- 每个阶段都有真实场景验证记录。
- 用户入口和 Agent 工具入口都可用。
- 危险操作有隔离、拒绝、回滚或保留审查机制。
- 自修改能力默认不污染主工作区。
- `/pursue` 能基于真实验证结果判断任务是否完成。
