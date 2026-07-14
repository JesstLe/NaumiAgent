# 持久目标 `/goal` 实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-durable-goal-design.md`

## 任务 1：领域模型与 SQLite Store

1. 先写 Store 定向测试，覆盖持久化、唯一约束、并发与状态机。
2. 实现 `Goal`、`GoalStatus`、`GoalStore` 和中文格式化。
3. 只运行 Goal Store 测试。

## 任务 2：Agent 工具与 Engine

1. 先写工具注册、输入校验、状态更新和 Pursuit 复用测试。
2. 实现 `goal_create/status/list/update/pursue`。
3. Engine 初始化 Store、注册工具，并给 HarnessContextInput 增加依赖。
4. 只运行 Goal 工具和 Context 测试。

## 任务 3：共享斜杠命令与 UI 目录

1. 先写 `/goal` 分发测试。
2. 实现 create/status/list/pause/resume/block/complete/cancel/pursue 解析。
3. 更新帮助、completer、CLI display 和 New UI slash command 目录。
4. 只运行 slash/bridge 相关测试。

## 任务 4：验证与提交

1. 重跑本功能定向 Python 测试。
2. Ruff、py_compile、git diff --check。
3. 审查并发唯一性、终态、权限路径、临时上下文和错误文案。
4. 以英文独立提交。
